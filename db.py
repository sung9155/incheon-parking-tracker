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


def latest(con: sqlite3.Connection) -> list[sqlite3.Row]:
    # 실제 API는 한 번의 폴에서도 층마다 datetm이 몇 초씩 어긋난다 — 전체가 같은 ts를
    # 공유한다고 가정하는 "ts = 전역 MAX(ts)"는 틀린다. 층별로 가장 최근 ts를 찾는다.
    # SQLite는 bare column(floor 제외 나머지)이 그 그룹의 MAX(ts) 행에서 온다는 것을
    # 문서로 보장한다: https://sqlite.org/lang_select.html#bareagg
    return con.execute(
        """
        SELECT MAX(ts) AS ts, floor, parked, capacity, capacity - parked AS available
        FROM parking
        GROUP BY floor
        """
    ).fetchall()


BUCKET_THRESHOLD_SECONDS = 3 * 86400
BUCKET_SECONDS = 3600
SHORT_BUCKET_SECONDS = 300  # 수집 주기와 맞춘다 — 한 번의 폴이 x축 한 점에 떨어지도록

_SERIES_BUCKETED = """
SELECT (ts / ?) * ? AS ts, floor, AVG(capacity - parked) AS available
FROM parking
WHERE ts BETWEEN ? AND ?
GROUP BY 1, floor
ORDER BY 1, floor
"""


def series(con: sqlite3.Connection, start: int, end: int) -> list[sqlite3.Row]:
    bucket = BUCKET_SECONDS if end - start > BUCKET_THRESHOLD_SECONDS else SHORT_BUCKET_SECONDS
    return con.execute(_SERIES_BUCKETED, (bucket, bucket, start, end)).fetchall()


_PATTERN = """
SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS dow,
       CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,
       floor,
       AVG(capacity - parked) AS available,
       COUNT(*) AS samples
FROM parking
GROUP BY dow, hour, floor
ORDER BY dow, hour, floor
"""


def pattern(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(_PATTERN).fetchall()
