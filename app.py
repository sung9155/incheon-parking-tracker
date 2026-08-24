import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import unquote

import httpx
from fastapi import FastAPI
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


# scripts/probe.py 출력(실제 API 호출, 2026-08-24)으로 확정한 floor 원문 → (터미널, 유형).
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


COLLECT_INTERVAL_SECONDS = 300


async def collect_once(client: httpx.AsyncClient, con, key: str) -> int:
    r = await client.get(
        API_URL,
        params={"serviceKey": key, "numOfRows": 100, "pageNo": 1, "type": "json"},
        timeout=15,
    )
    r.raise_for_status()
    rows = parse_rows(r.json())
    for _, floor, _, _ in rows:
        group_of(floor)          # 미매핑 구역 경고를 남긴다
    db.insert_rows(con, rows)
    return len(rows)


def _log_collect_failure(exc: Exception) -> None:
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
        log.error("collect failed: HTTP %s for %s", status, url)
    elif url is not None:
        log.error("collect failed: %s for %s", type(exc).__name__, url)
    else:
        log.error("collect failed: %s", type(exc).__name__)


async def collect_loop(con) -> None:
    # .env에는 Encoding 키 또는 Decoding 키가 들어올 수 있다. httpx params=는 값을 다시
    # URL 인코딩하므로, Encoding 키를 그대로 넘기면 이중 인코딩되어 403이 난다. unquote는
    # Decoding 키(base64, %가 없음)에는 no-op이고, Encoding 키는 되돌려 올바르게 인코딩되게 한다.
    key = unquote(os.environ["SERVICE_KEY"])
    async with httpx.AsyncClient() as client:
        while True:
            try:
                n = await collect_once(client, con, key)
                log.info("collected %d rows", n)
            except Exception as e:
                # 재시도하지 않는다. 5분 뒤 다음 틱이 온다.
                _log_collect_failure(e)
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    con = db.connect()
    _app.state.con = con
    task = None
    if os.environ.get("COLLECT", "1") == "1":
        task = asyncio.create_task(collect_loop(con))
    yield
    if task is not None:
        task.cancel()
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
def api_series(days: int = 1):
    end = int(time.time())
    return [_with_group(r) for r in db.series(app.state.con, end - days * 86400, end)]


@app.get("/api/pattern")
def api_pattern():
    return [_with_group(r) for r in db.pattern(app.state.con)]


app.mount("/", StaticFiles(directory="static", html=True), name="static")
