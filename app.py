import asyncio
import csv
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import NamedTuple
from datetime import date, datetime, time as clock, timedelta
from urllib.parse import unquote

import holidays
import httpx
from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import db

log = logging.getLogger("parking")

API_URL = "https://apis.data.go.kr/B551177/StatusOfParking/getTrackingParking"

# 순서 주의: 프로덕션이 실제로 보내는 포맷("%Y%m%d%H%M%S.%f", 초 단위 소수점 포함)이
# 맨 앞. 뒤이은 두 압축 포맷은 12자리("%Y%m%d%H%M")가 14자리("%Y%m%d%H%M%S")보다
# 먼저 와야 한다 — strptime은 %M/%S가 자릿수 제한 없이 그리디하게 매칭하므로, 14자리
# 입력을 "%Y%m%d%H%M%S"로 먼저 시도하면 "202608241305"의 마지막 두 자리가 초로
# 잘못 흡수되어 자정 근처가 아니어도 5분 단위 데이터가 조용히 어긋난다.
DATETM_FORMATS = (
    "%Y%m%d%H%M%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y%m%d%H%M",
    "%Y%m%d%H%M%S",
)


def parse_datetm(s: str) -> int:
    s = s.strip()
    for fmt in DATETM_FORMATS:
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"unknown datetm format: {s!r}")


def parse_rows(payload: dict) -> list[tuple[int, str, int, int]]:
    items = payload["response"]["body"]["items"]
    if isinstance(items, dict):
        items = items.get("item", [])
    # 한 응답 안에서도 구역별 datetm이 최대 23초까지 어긋난다 (측정치, 신호 아님).
    # 이를 각 행에 그대로 쓰면 300초 버킷 경계 근처에서 한 폴이 두 버킷으로 쪼개져,
    # 프론트가 부분 합계를 그래프에 그린다. 응답 전체를 그 응답의 최대 datetm 하나로
    # 뭉갠다 — 응답이 그대로면 max도 그대로라 INSERT OR IGNORE 중복 제거는 그대로 유지된다.
    parsed = [
        (
            parse_datetm(it["datetm"]),
            it["floor"].strip(),
            int(it["parking"]),
            int(it["parkingarea"]),
        )
        for it in items
    ]
    if not parsed:
        return []
    ts = max(row[0] for row in parsed)
    return [(ts, floor, parked, capacity) for _, floor, parked, capacity in parsed]


# 실제 API 호출(2026-08-24)로 확정한 floor 원문 → (터미널, 유형).
# 유형은 단기/장기 외에 예약주차장(예약)이 있다 — 데이터셋 설명에는 없던 실제 3번째 유형.
FLOOR_GROUPS: dict[str, tuple[str, str]] = {
    "T1 단기주차장지하1층": ("T1", "단기"),
    "T1 단기주차장지하2층": ("T1", "단기"),
    "T1 단기주차장지하3층": ("T1", "단기"),
    "T1 단기주차장지상층": ("T1", "단기"),
    "T1 장기 P1 주차장": ("T1", "장기"),
    "T1 장기 P1 주차타워": ("T1", "장기"),
    "T1 장기 P2 주차장": ("T1", "장기"),
    "T1 장기 P2 주차타워": ("T1", "장기"),
    "T1 장기 P3 주차장": ("T1", "장기"),
    "T1 P5 예약주차장": ("T1", "예약"),
    "T2 단기주차장지하M층": ("T2", "단기"),
    "T2 단기주차장지상1층": ("T2", "단기"),
    "T2 단기주차장지상2층": ("T2", "단기"),
    "T2 단기주차장지상3층": ("T2", "단기"),
    "T2 단기주차장지상4층": ("T2", "단기"),
    "T2 장기 주차장": ("T2", "장기"),
    "T2 P1 장기주차타워": ("T2", "장기"),
    "T2 P2 장기주차타워": ("T2", "장기"),
    "T2 예약 주차장": ("T2", "예약"),
}

# ponytail: unbounded set, but the API returns a fixed 19 labels — bound it if that ever changes
_warned_floors: set[str] = set()


def group_of(floor: str) -> tuple[str, str]:
    group = FLOOR_GROUPS.get(floor)
    if group is None:
        if floor not in _warned_floors:
            _warned_floors.add(floor)
            log.warning("unmapped floor %r — grouped as 기타", floor)
        return ("기타", "기타")
    return group


# ------------------------------------------------------------- 승객 예고
#
# 활용가이드 V5.0 (2025-10-30) 기준. 필드명이 t1eg1..4처럼 번호로 보이지만 실제로는
# 구역 이름이다 — 번호로 다루면 화면의 "입국장 2번"이 실제 E·F 구역을 가리키게 되어
# 공항 안내판과 어긋난다.
PASSENGER_API_URL = "https://apis.data.go.kr/B551177/passgrAnncmt/getPassgrAnncmt"

PASSENGER_FIELDS: dict[str, tuple[str, str, str]] = {
    "t1eg1": ("T1", "입국", "A·B"),
    "t1eg2": ("T1", "입국", "E·F"),
    "t1eg3": ("T1", "입국", "C"),
    "t1eg4": ("T1", "입국", "D"),
    "t1dg1": ("T1", "출국", "1"),
    "t1dg2": ("T1", "출국", "2"),
    "t1dg3": ("T1", "출국", "3"),
    "t1dg4": ("T1", "출국", "4"),
    "t1dg5": ("T1", "출국", "5"),
    # 가이드 주석: T1 6번 출국장은 교통약자 우대 출구로 공항 예상혼잡도 대상에서 제외된다.
    "t1dg6": ("T1", "출국", "6"),
    "t2eg1": ("T2", "입국", "A"),
    "t2eg2": ("T2", "입국", "B"),
    "t2dg1": ("T2", "출국", "1"),
    "t2dg2": ("T2", "출국", "2"),
}
# 합계 필드(t1egsum1 / t1dgsum1 / t2egsum1 / t2dgsum2)는 일부러 담지 않는다. 게이트에서
# 더하면 나오는 값이고, 둘 다 저장하면 언젠가 서로 어긋난다. 참고로 T2만 접미사가
# sum2인데 이는 API 자체의 표기 불일치다.


def parse_passengers(payload: dict) -> list[tuple[str, int, str, str, str, int]]:
    """(adate, hour, terminal, direction, gate, expected) 튜플들."""
    items = payload["response"]["body"]["items"]
    if isinstance(items, dict):
        items = items.get("item", [])
    out = []
    for it in items:
        adate, atime = it.get("adate", ""), it.get("atime", "")
        # 응답에는 시간대 24행 외에 '합계' 행이 섞여 온다. 그대로 담으면 이중 계산이다.
        if not adate.isdigit() or "_" not in atime:
            continue
        day = f"{adate[:4]}-{adate[4:6]}-{adate[6:8]}"
        hour = int(atime.split("_")[0])
        for field, (terminal, direction, gate) in PASSENGER_FIELDS.items():
            out.append((day, hour, terminal, direction, gate, int(float(it[field]))))
    return out


async def collect_passengers(client: httpx.AsyncClient, con, key: str) -> int:
    """오늘(selectdate=0)과 내일(=1)을 각각 받는다. 내일치가 곧 예측 화면이 된다."""
    seen_at, total = int(time.time()), 0
    for selectdate in (0, 1):
        r = await client.get(
            PASSENGER_API_URL,
            # numOfRows가 totalCount보다 작으면 데이터가 조용히 잘린다. 가이드가 명시적으로
            # 경고하는 부분으로, 기본값 10을 쓰면 24시간 중 10시간만 온다.
            params={"serviceKey": key, "numOfRows": 100, "pageNo": 1,
                    "type": "json", "selectdate": selectdate},
            timeout=15,
        )
        r.raise_for_status()
        payload = None
        try:
            payload = r.json()
            rows = parse_passengers(payload)
        except (KeyError, ValueError, TypeError):
            log.error("passengers failed to parse response: %s", _result_msg(payload))
            raise
        db.upsert_passengers(con, rows, seen_at)
        total += len(rows)
    return total


COLLECT_INTERVAL_SECONDS = 300


async def collect_once(client: httpx.AsyncClient, con, key: str) -> int:
    r = await client.get(
        API_URL,
        # numOfRows=100: 오늘 기준 19개 구역 전부가 한 호출에 온다. 공항이 100개를
        # 넘어서면 조용히 잘리므로, 그때는 이 값을 올려야 한다.
        params={"serviceKey": key, "numOfRows": 100, "pageNo": 1, "type": "json"},
        timeout=15,
    )
    r.raise_for_status()
    payload = None
    try:
        payload = r.json()
        rows = parse_rows(payload)
    except (KeyError, ValueError, TypeError):
        # 일일 쿼터 소진 시 data.go.kr은 HTTP 200 + 에러 바디를 준다 — 그러면 여기서
        # KeyError나 JSONDecodeError가 난다. resultMsg를 남겨 원인을 알 수 있게 한다.
        log.error("collect failed to parse response: %s", _result_msg(payload))
        raise
    for _, floor, _, _ in rows:
        group_of(floor)          # 미매핑 구역 경고를 남긴다
    db.insert_rows(con, rows)
    return len(rows)


def _result_msg(payload: dict) -> str:
    # 일일 쿼터 소진 시 data.go.kr은 HTTP 200 + 에러 바디를 준다. 여기서 흔히 파싱이
    # 실패하는데, 그 원인 메시지(resultMsg)를 남겨 "쿼터 소진"과 "응답 형식 변경"을
    # 구분할 수 있게 한다. payload 형태가 예상과 다를 수 있으므로 이 함수 자체는 절대
    # 예외를 던지지 않는다 — collect_once의 except 경로 안에서 호출되기 때문이다.
    try:
        return payload["response"]["header"]["resultMsg"]
    except Exception:
        return "no resultMsg"


def _log_collect_failure(exc: Exception, source: str = "collect") -> None:
    # 이 함수는 절대 예외를 던지면 안 된다 — collect_loop의 except 안에서 호출되므로,
    # 여기서 터지면 그 예외가 루프 밖으로 새어나가 수집기 태스크가 영구히 죽는다.
    # (예: httpx.DecodingError는 httpx.HTTPError의 서브클래스이지만 .request가 안 붙은 채로
    # 만들어질 수 있어, exc.request에 그냥 접근하면 RuntimeError가 난다.)
    #
    # 또한 어떤 필드도 무심코 요청 URL의 쿼리스트링(=serviceKey)을 로그에 흘리면 안 된다.
    # str(exc)/log.exception()의 트레이스백에는 그 URL이 그대로 들어갈 수 있으므로 절대
    # 원본 예외 메시지를 로깅하지 않는다. "이 예외 타입은 안전하다"를 나열하는 대신,
    # 상태 코드/쿼리 없는 URL/타입명만 뽑아내는 이 경로 자체를 무엇을 받아도 안전하게 만든다
    # — 그래야 나중에 새로운 예외 타입이 나타나도 이 보장이 깨지지 않는다.
    status = url = None
    try:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        url = exc.request.url.copy_with(query=None)
    except Exception:
        pass

    if status is not None and url is not None:
        log.error("%s failed: HTTP %s for %s", source, status, url)
    elif url is not None:
        log.error("%s failed: %s for %s", source, type(exc).__name__, url)
    else:
        log.error("%s failed: %s", source, type(exc).__name__)


class Source(NamedTuple):
    """수집 대상 하나. 소스를 늘리는 일은 SOURCES에 한 줄 추가하는 것이어야 한다.

    루프를 복제하기 시작하면 재시도 정책·로깅·종료 처리가 소스마다 갈라진다.
    """
    name: str
    interval: int                       # 초
    collect: object                     # async (client, con, key) -> 삽입 시도한 행 수
    last_ts: object                     # (con) -> 마지막 수집 epoch 또는 None


def _parking_last_ts(con):
    return con.execute("SELECT MAX(ts) FROM parking").fetchone()[0]


# 공공데이터포털은 데이터셋마다 트래픽 한도가 따로다. 소스를 늘려도 서로 깎아먹지 않는다.
def _passengers_last_ts(con):
    return con.execute("SELECT MAX(updated) FROM passengers").fetchone()[0]


SOURCES = [
    Source("parking", COLLECT_INTERVAL_SECONDS, lambda c, con, k: collect_once(c, con, k),
           _parking_last_ts),
    # 갱신 주기가 주차와 같은 5분이라 주기를 맞춘다 — 두 데이터를 같은 시각 축에서
    # 비교하기도 편하다. 호출은 오늘·내일 2회라 하루 576회, 한도 1,000 안이다.
    Source("passengers", COLLECT_INTERVAL_SECONDS, collect_passengers, _passengers_last_ts),
]


async def collect_loop(source: "Source", con, key: str) -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                n = await source.collect(client, con, key)
                log.info("%s: collected %d rows", source.name, n)
            except Exception as e:
                # 재시도하지 않는다. 다음 틱이 온다.
                _log_collect_failure(e, source.name)
            await asyncio.sleep(source.interval)


# ---------------------------------------------------------------- 날짜 · 공휴일

# 제헌절은 2008년부터 공휴일이 아니다. holidays 패키지는 아직 포함하고 있어서, 그대로
# 두면 제헌절이 금요일인 해마다 금·토·일이 가짜 "황금연휴"로 잡힌다.
NOT_A_DAY_OFF = frozenset({"제헌절"})

# 연휴로 치는 최소 연속 일수. 주말 이틀만으로는 잡히지 않는다.
GOLDEN_HOLIDAY_MIN_DAYS = 3

# 요청 구간에 걸친 연휴는 잘리지 않고 통째로 반환해야 차트 음영이 온전하다. 구간 밖으로
# 얼마나 더 훑을지 — 한국에서 가장 긴 연휴도 이 안에 들어온다.
_RUN_SCAN_PAD_DAYS = 12

_NAME_SUFFIXES = (" 전날", " 다음날", " 대체 휴일", " 대체휴일")


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def day_range_epoch(from_value: str, to_value: str) -> tuple[int, int]:
    """`YYYY-MM-DD` 두 개를 그 날들을 온전히 덮는 epoch 구간으로 바꾼다.

    parse_datetm과 같은 규약으로 로컬 시간대(컨테이너의 TZ=Asia/Seoul) 자정을 쓴다.
    """
    first, last = parse_date(from_value), parse_date(to_value)
    start = int(datetime.combine(first, clock.min).timestamp())
    end = int(datetime.combine(last, clock.max).timestamp())
    return start, end


def _base_name(name: str) -> str:
    """'설날 다음날' -> '설날', '신정연휴' -> '신정'."""
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    if name.endswith("연휴") and len(name) > 2:
        return name[:-2]
    return name


def golden_holidays(from_value: str, to_value: str) -> list[dict]:
    """구간에 걸친 황금연휴 목록. 주말과 공휴일이 연속으로 이어지는 구간을 묶는다."""
    first, last = parse_date(from_value), parse_date(to_value)
    scan_from = first - timedelta(days=_RUN_SCAN_PAD_DAYS)
    scan_to = last + timedelta(days=_RUN_SCAN_PAD_DAYS)
    calendar = holidays.country_holidays(
        "KR", years=list(range(scan_from.year, scan_to.year + 1)), language="ko"
    )

    runs, run = [], []
    day = scan_from
    while day <= scan_to:
        name = calendar.get(day)
        # 기본 이름으로 걸러야 한다 — "제헌절 대체 휴일"처럼 접미사가 붙은 형태는
        # 정확 일치로는 빠져나간다(제헌절이 토요일인 해에 실제로 그렇게 된다).
        if name is not None and _base_name(name) in NOT_A_DAY_OFF:
            name = None
        if name is not None or day.weekday() >= 5:      # 5,6 = 토,일
            run.append((day, name))
        elif run:
            runs.append(run)
            run = []
        day += timedelta(days=1)
    if run:
        runs.append(run)

    out = []
    for entry in runs:
        if len(entry) < GOLDEN_HOLIDAY_MIN_DAYS:
            continue
        start, end = entry[0][0], entry[-1][0]
        if end < first or start > last:                  # 구간과 겹치지 않는다
            continue
        names = []
        for _, name in entry:
            if name and (base := _base_name(name)) not in names:
                names.append(base)
        out.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "name": "·".join(names) or "주말",
            "days": len(entry),
        })
    return out


def _attach_log_handler() -> None:
    """uvicorn이 로깅을 설정한 뒤에 호출해야 한다.

    uvicorn은 기동 시 dictConfig로 자기 로거들만 설정하고 root에는 핸들러를 두지
    않는다. 그래서 "parking" 로거는 실효 레벨 WARNING에 핸들러가 없는 상태가 되어,
    log.error는 logging.lastResort로 stderr에 찍히지만 log.info("collected N rows")는
    통째로 버려진다. 무인 운영에서 유일한 성공 신호가 로그에 영원히 안 나오는 셈이다.

    import 시점에 basicConfig를 부르는 방법은 통하지 않는다 — uvicorn의 dictConfig가
    나중에 실행되면서 root 핸들러를 정리한다. 그래서 lifespan에서 우리 로거에만 붙인다.
    """
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _attach_log_handler()
    con = db.connect()
    _app.state.con = con
    tasks = []
    if os.environ.get("COLLECT", "1") == "1":
        # 여기서 읽는다(수집 루프 안이 아니라) — 키가 없으면 컨테이너가 바로 죽어
        # restart: unless-stopped로 눈에 띄게 한다. 루프 안에서 읽으면 수집 태스크만
        # 조용히 죽고 웹 UI는 계속 200을 반환해 아무도 못 알아챈다.
        #
        # .env에는 Encoding 키 또는 Decoding 키가 들어올 수 있다. httpx params=는 값을
        # 다시 URL 인코딩하므로, Encoding 키를 그대로 넘기면 이중 인코딩되어 403이 난다.
        # unquote는 Decoding 키(base64, %가 없음)에는 no-op이고, Encoding 키는 되돌려
        # 올바르게 인코딩되게 한다.
        raw_key = os.environ.get("SERVICE_KEY", "")
        if not raw_key:
            raise RuntimeError("SERVICE_KEY is required (set it in .env)")
        key = unquote(raw_key)
        tasks = [asyncio.create_task(collect_loop(src, con, key)) for src in SOURCES]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    con.close()


app = FastAPI(lifespan=lifespan)


def _with_group(row) -> dict:
    d = dict(row)
    d["terminal"], d["kind"] = group_of(d["floor"])
    return d


@app.get("/api/current")
def api_current():
    return [_with_group(r) for r in db.latest(app.state.con)]


@app.get("/api/series")
def api_series(
    from_value: str = Query(alias="from"),
    to_value: str = Query(alias="to"),
    bucket: str = Query(default="auto"),
):
    start, end = day_range_epoch(from_value, to_value)
    # 알 수 없는 값은 자동으로 흘려보낸다 — 조회 해상도 때문에 화면이 죽을 이유는 없다.
    size = int(bucket) if bucket.isdigit() and int(bucket) in db.BUCKETS else None
    used = db.auto_bucket(start, end) if size is None else db.clamp_bucket(start, end, size)
    rows = [_with_group(r) for r in db.series(app.state.con, start, end, size)]
    # 프론트가 x축을 버킷 단위로 촘촘히 채우려면 실제로 쓰인 버킷 크기를 알아야 한다.
    # 그래야 수집이 끊긴 구간이 직선으로 이어지지 않고 빈 칸으로 남는다.
    return JSONResponse(rows, headers={"X-Bucket-Seconds": str(used)})


@app.get("/api/holidays")
def api_holidays(
    from_value: str = Query(alias="from"),
    to_value: str = Query(alias="to"),
):
    return golden_holidays(from_value, to_value)


# "평소"를 계산할 때 되돌아볼 기간. 3년 전 주차 패턴은 평소가 아니고, 전체 스캔은
# 이력이 쌓일수록 느려진다. 두 문제가 같은 한 줄로 해결된다.
PATTERN_WINDOW_DAYS = 180


def pattern_exclusions(since: int, until: int) -> set[str]:
    """창 안의 황금연휴 날짜들. 평균에서 빼야 '평소'가 평소가 된다."""
    days = set()
    first = datetime.fromtimestamp(since).date().isoformat()
    last = datetime.fromtimestamp(until).date().isoformat()
    for run in golden_holidays(first, last):
        day = parse_date(run["start"])
        stop = parse_date(run["end"])
        while day <= stop:
            days.add(day.isoformat())
            day += timedelta(days=1)
    return days


@app.get("/api/pattern")
def api_pattern():
    now = int(time.time())
    since = now - PATTERN_WINDOW_DAYS * 86400
    rows = db.pattern(app.state.con, since=since,
                      exclude_days=pattern_exclusions(since, now))
    return [_with_group(r) for r in rows]


# 마지막 수집이 이보다 오래됐으면 수집기가 죽은 것으로 본다. 수집 주기(300초)의
# 여러 배 — 한두 번 실패로 경보가 울리면 안 되지만, 한 시간 넘게 조용하면 문제다.
HEALTH_STALE_AFTER_SECONDS = 3600


@app.get("/api/health")
def api_health(response: Response):
    """모니터링이 걸 수 있는 신호. 카드가 비는 것 말고는 수집기 사망을 알 길이 없다.

    소스마다 따로 보고한다 — 하나가 죽고 나머지가 멀쩡하면 전체를 ok로 묶어선 안 된다.
    """
    con, now = app.state.con, int(time.time())
    sources, all_ok = {}, True
    for src in SOURCES:
        last = src.last_ts(con)
        age = None if last is None else now - last
        ok = age is not None and age <= HEALTH_STALE_AFTER_SECONDS
        all_ok = all_ok and ok
        sources[src.name] = {
            "status": "ok" if ok else "stale",
            "last_collect": None if last is None else datetime.fromtimestamp(last).isoformat(),
            "age_seconds": age,
            "interval_seconds": src.interval,
        }

    if not all_ok:
        # 200을 돌려주면 Uptime Kuma 같은 도구가 정상으로 읽는다.
        response.status_code = 503

    parking = con.execute(
        "SELECT MAX(ts) AS last, COUNT(*) AS rows, COUNT(DISTINCT floor) AS floors FROM parking"
    ).fetchone()
    return {
        "status": "ok" if all_ok else "stale",
        "sources": sources,
        # 아래 셋은 기존 모니터링 설정이 참조하고 있을 수 있어 남긴다.
        "last_collect": sources["parking"]["last_collect"],
        "age_seconds": sources["parking"]["age_seconds"],
        "rows": parking["rows"],
        "floors": parking["floors"],
        # TZ가 빠지면 저장되는 모든 ts가 밀린다. 여기서 바로 확인할 수 있게 노출한다.
        "localtime_epoch_zero": con.execute(
            "SELECT datetime(0, 'unixepoch', 'localtime')"
        ).fetchone()[0],
    }


@app.get("/api/export.csv")
def api_export(
    from_value: str = Query(alias="from"),
    to_value: str = Query(alias="to"),
):
    """구간의 원본 행을 CSV로. 버킷 평균이 아니라 원본이어야 백업이 된다.

    공공 API가 당일치만 주므로 이 DB가 사라지면 이력은 복구할 방법이 없다.
    """
    start, end = day_range_epoch(from_value, to_value)
    buf = io.StringIO()
    out = csv.writer(buf, lineterminator=chr(10))
    out.writerow(["datetime", "ts", "floor", "parked", "capacity", "available"])
    for r in app.state.con.execute(
        "SELECT ts, floor, parked, capacity FROM parking "
        "WHERE ts BETWEEN ? AND ? ORDER BY ts, floor", (start, end)
    ):
        out.writerow([
            datetime.fromtimestamp(r["ts"]).isoformat(sep=" "),
            r["ts"], r["floor"], r["parked"], r["capacity"],
            r["capacity"] - r["parked"],
        ])
    name = f"incheon-parking_{from_value}_{to_value}.csv"
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
