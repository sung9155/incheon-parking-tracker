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
    return con.execute(
        """
        SELECT ts, floor, parked, capacity, capacity - parked AS available
        FROM parking
        WHERE ts = (SELECT MAX(ts) FROM parking)
        """
    ).fetchall()


BUCKET_THRESHOLD_SECONDS = 3 * 86400
BUCKET_SECONDS = 3600

_SERIES_RAW = """
SELECT ts, floor, capacity - parked AS available
FROM parking
WHERE ts BETWEEN ? AND ?
ORDER BY ts, floor
"""

_SERIES_BUCKETED = """
SELECT (ts / ?) * ? AS ts, floor, AVG(capacity - parked) AS available
FROM parking
WHERE ts BETWEEN ? AND ?
GROUP BY 1, floor
ORDER BY 1, floor
"""


def series(con: sqlite3.Connection, start: int, end: int) -> list[sqlite3.Row]:
    if end - start > BUCKET_THRESHOLD_SECONDS:
        return con.execute(
            _SERIES_BUCKETED, (BUCKET_SECONDS, BUCKET_SECONDS, start, end)
        ).fetchall()
    return con.execute(_SERIES_RAW, (start, end)).fetchall()


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
