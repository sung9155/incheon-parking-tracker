import os
import sqlite3
from pathlib import Path
from typing import Iterable

DEFAULT_DB_PATH = "data/parking.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS parking (
  ts       INTEGER NOT NULL,
  floor    TEXT    NOT NULL,
  parked   INTEGER NOT NULL,
  capacity INTEGER NOT NULL,
  PRIMARY KEY (ts, floor)
) WITHOUT ROWID;

-- 승객 예고. 주차와 달리 같은 (날짜, 시간대)의 값이 갱신되므로 INSERT OR REPLACE로
-- 최신 예고가 이긴다. 게이트 단위로 저장한다 — 합계는 언제든 더하면 나오지만 게이트를
-- 버리면 되돌릴 수 없고, 이 API도 오늘·내일치만 주므로 버린 해상도는 영영 못 찾는다.
CREATE TABLE IF NOT EXISTS passengers (
  adate     TEXT    NOT NULL,   -- 'YYYY-MM-DD'
  hour      INTEGER NOT NULL,   -- 0~23
  terminal  TEXT    NOT NULL,   -- 'T1' | 'T2'
  direction TEXT    NOT NULL,   -- '출국' | '입국'
  gate      TEXT    NOT NULL,   -- 공식 안내판 표기: '1'~'6', 'A·B', 'E·F', 'C', 'D', 'A', 'B'
  expected  INTEGER NOT NULL,
  updated   INTEGER NOT NULL,   -- 이 예고를 마지막으로 본 시각(epoch)
  PRIMARY KEY (adate, hour, terminal, direction, gate)
) WITHOUT ROWID;

-- 출국장 혼잡도. 1~2분 주기로 갱신되는 실측이라 주차처럼 시각별로 쌓는다.
CREATE TABLE IF NOT EXISTS congestion (
  ts           INTEGER NOT NULL,   -- occurtime(측정 시각) epoch
  terminal     TEXT    NOT NULL,   -- 'T1' | 'T2'
  gate         TEXT    NOT NULL,   -- T1: DG1_E..DG6_W (동/서), T2: DG1_A..DG2_D (입구)
  wait_minutes INTEGER NOT NULL,
  wait_people  INTEGER NOT NULL,
  wait_capped  INTEGER NOT NULL,   -- API가 '60+'로만 줬으면 1 — 62분과 3시간이 같아 보이면 안 된다
  operating    TEXT    NOT NULL,   -- '05:00~22:00' 등. 빈 문자열이면 그 시각 미운영
  PRIMARY KEY (ts, terminal, gate)
) WITHOUT ROWID;
"""


def connect(path=None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("DB_PATH") or DEFAULT_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def insert_rows(con: sqlite3.Connection, rows: Iterable[tuple[int, str, int, int]]) -> None:
    con.executemany(
        "INSERT OR IGNORE INTO parking (ts, floor, parked, capacity) VALUES (?, ?, ?, ?)",
        rows,
    )
    con.commit()


LATEST_MAX_AGE_SECONDS = 3600  # 이보다 오래된 floor는 "현재"로 취급하지 않는다


def upsert_passengers(con: sqlite3.Connection, rows, seen_at: int) -> None:
    """예고는 갱신된다 — REPLACE로 최신 값이 이긴다 (주차의 IGNORE와 반대)."""
    con.executemany(
        "INSERT OR REPLACE INTO passengers "
        "(adate, hour, terminal, direction, gate, expected, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(*r, seen_at) for r in rows],
    )
    con.commit()


def passengers(con: sqlite3.Connection, first: str, last: str) -> list[sqlite3.Row]:
    """날짜 구간의 시간대별 예고. 'YYYY-MM-DD' 문자열 비교로 충분하다."""
    return con.execute(
        "SELECT adate, hour, terminal, direction, gate, expected FROM passengers "
        "WHERE adate BETWEEN ? AND ? ORDER BY adate, hour, terminal, direction, gate",
        (first, last),
    ).fetchall()


def upsert_congestion(con: sqlite3.Connection, rows) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO congestion "
        "(ts, terminal, gate, wait_minutes, wait_people, wait_capped, operating) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()


def congestion_latest(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """게이트마다 가장 최근 측정. 터미널마다 occurtime이 달라 전역 MAX로는 한쪽만 남는다."""
    return con.execute(
        "SELECT MAX(ts) AS ts, terminal, gate, wait_minutes, wait_people, wait_capped, operating "
        "FROM congestion GROUP BY terminal, gate ORDER BY terminal, gate"
    ).fetchall()


def congestion_series(con: sqlite3.Connection, start: int, end: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT ts, terminal, gate, wait_minutes, wait_people FROM congestion "
        "WHERE ts BETWEEN ? AND ? ORDER BY ts, terminal, gate", (start, end)
    ).fetchall()


def latest(con: sqlite3.Connection) -> list[sqlite3.Row]:
    # 실제 API는 한 번의 폴에서도 층마다 datetm이 몇 초씩 어긋난다 — 전체가 같은 ts를
    # 공유한다고 가정하는 "ts = 전역 MAX(ts)"는 틀린다. 층별로 가장 최근 ts를 찾는다.
    # SQLite는 bare column(floor 제외 나머지)이 그 그룹의 MAX(ts) 행에서 온다는 것을
    # 문서로 보장한다: https://sqlite.org/lang_select.html#bareagg
    #
    # WHERE로 최근 1시간 안에 갱신되지 않은 floor는 제외한다 — 그렇지 않으면 API에서
    # 사라진 구역(퇴역, 또는 키 만료로 수집기가 죽은 경우)이 마지막 값을 영원히 "현재"로
    # 보고한다. HAVING MAX(ts) >= ...가 아니라 WHERE ts >= ...인 이유: 어떤 그룹이든
    # 이 윈도우 안에 걸리는 행이 하나라도 있으면 그 그룹의 MAX(ts)도 당연히 그 윈도우
    # 안에 있으므로(윈도우 밖의 오래된 행은 애초에 MAX가 될 수 없다) 두 표현은 동치다.
    # WHERE는 (ts, floor) 기본키를 타서 전체 스캔을 피한다 — 1년치(199만 행) 합성
    # 테이블에서 실측: HAVING 1.688s vs WHERE 0.001s, 반환 행은 동일.
    return con.execute(
        """
        SELECT MAX(ts) AS ts, floor, parked, capacity, capacity - parked AS available
        FROM parking
        WHERE ts >= strftime('%s', 'now') - ?
        GROUP BY floor
        """,
        (LATEST_MAX_AGE_SECONDS,),
    ).fetchall()


# 고를 수 있는 조회 단위. 맨 아래는 수집 주기(300초)와 같다 — 한 번의 폴이 x축 한 점에
# 떨어지므로 이보다 잘게 쪼개는 것은 의미가 없다.
BUCKETS = (300, 600, 1800, 3600, 21600, 86400)

# 자동 선택이 지키는 목표치. 아무것도 고르지 않은 사람에게는 가볍고 빠른 쪽이 낫다.
TARGET_POINTS = 1500

# 사용자가 단위를 직접 골랐을 때 허용하는 상한. 자동보다 넉넉하다 — 직접 고른 해상도를
# 조용히 내려버리면 고른 의미가 없다. 이 선을 넘을 때만(예: 30일치 5분 = 8,640포인트)
# 굵은 쪽으로 내린다.
MAX_POINTS = 6000


def _finest_within(span: int, limit: int) -> int:
    for bucket in BUCKETS:
        if span / bucket <= limit:
            return bucket
    return BUCKETS[-1]


def auto_bucket(start: int, end: int) -> int:
    """포인트 수가 목표를 넘지 않는 선에서 가장 촘촘한 단위를 고른다.

    "3일 넘으면 무조건 1시간" 같은 고정 임계값을 쓰지 않는다 — 그러면 7일 조회가
    5분 수집 데이터를 시간 단위로 뭉개버려, 실제로 측정한 해상도를 버리게 된다.
    """
    return _finest_within(max(end - start, 1), TARGET_POINTS)


def clamp_bucket(start: int, end: int, bucket: int) -> int:
    """직접 고른 단위를 존중하되, 감당 못 할 양이 되면 굵은 쪽으로 내린다."""
    return max(bucket, _finest_within(max(end - start, 1), MAX_POINTS))

_SERIES_BUCKETED = """
SELECT (ts / ?) * ? AS ts, floor, AVG(capacity - parked) AS available,
       AVG(capacity) AS capacity
FROM parking
WHERE ts BETWEEN ? AND ?
GROUP BY 1, floor
ORDER BY 1, floor
"""


def series(con: sqlite3.Connection, start: int, end: int,
           bucket: int | None = None) -> list[sqlite3.Row]:
    # 반환되는 ts는 버킷의 "바닥"이라 start보다 작을 수 있다 (예: series(400, 600) -> ts=300).
    # off-by-one이 아니라 의도된 동작 — ts는 그 버킷을 대표하는 값이다.
    bucket = auto_bucket(start, end) if bucket is None else clamp_bucket(start, end, bucket)
    return con.execute(_SERIES_BUCKETED, (bucket, bucket, start, end)).fetchall()


def pattern_sql(since: int | None, exclude_days: set[str]) -> str:
    """요일×시간 평균 쿼리를 만든다.

    `since`가 있으면 PK (ts, floor)의 범위 스캔으로 끝난다 — 없으면 전체 테이블을
    훑으므로 이력이 몇 년 쌓이면 클릭 한 번에 초 단위로 멈춘다.

    `exclude_days`는 로컬 날짜 문자열 집합이다. 황금연휴를 빼기 위한 것으로, 연휴가
    섞이면 "평소"가 평소가 아니게 되어 기준선이 위로 끌려 올라간다.
    """
    where = []
    if since is not None:
        where.append("ts >= ?")
    if exclude_days:
        holes = ", ".join("?" for _ in exclude_days)
        where.append(f"date(ts, 'unixepoch', 'localtime') NOT IN ({holes})")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return f"""
SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS dow,
       CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,
       floor,
       AVG(capacity - parked) AS available,
       AVG(capacity) AS capacity,
       COUNT(*) AS samples
FROM parking
{clause}
GROUP BY dow, hour, floor
ORDER BY dow, hour, floor
"""


def pattern(con: sqlite3.Connection, since: int | None = None,
            exclude_days: set[str] | None = None) -> list[sqlite3.Row]:
    exclude_days = exclude_days or set()
    params: list = ([since] if since is not None else []) + sorted(exclude_days)
    return con.execute(pattern_sql(since, exclude_days), params).fetchall()
