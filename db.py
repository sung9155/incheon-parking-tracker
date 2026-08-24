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


BUCKET_THRESHOLD_SECONDS = 3 * 86400
BUCKET_SECONDS = 3600
SHORT_BUCKET_SECONDS = 300  # 수집 주기와 맞춘다 — 한 번의 폴이 x축 한 점에 떨어지도록

_SERIES_BUCKETED = """
SELECT (ts / ?) * ? AS ts, floor, AVG(capacity - parked) AS available,
       AVG(capacity) AS capacity
FROM parking
WHERE ts BETWEEN ? AND ?
GROUP BY 1, floor
ORDER BY 1, floor
"""


def series(con: sqlite3.Connection, start: int, end: int) -> list[sqlite3.Row]:
    # 반환되는 ts는 버킷의 "바닥"이라 start보다 작을 수 있다 (예: series(400, 600) -> ts=300).
    # off-by-one이 아니라 의도된 동작 — ts는 그 버킷을 대표하는 값이다.
    bucket = BUCKET_SECONDS if end - start > BUCKET_THRESHOLD_SECONDS else SHORT_BUCKET_SECONDS
    return con.execute(_SERIES_BUCKETED, (bucket, bucket, start, end)).fetchall()


_PATTERN = """
SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS dow,
       CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,
       floor,
       AVG(capacity - parked) AS available,
       AVG(capacity) AS capacity,
       COUNT(*) AS samples
FROM parking
GROUP BY dow, hour, floor
ORDER BY dow, hour, floor
"""


def pattern(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(_PATTERN).fetchall()
