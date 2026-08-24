from datetime import datetime

import pytest

import db
import app


def test_insert_is_idempotent_on_same_ts_and_floor(tmp_path):
    con = db.connect(tmp_path / "t.db")
    rows = [(1000, "단기주차장 지상", 50, 100)]

    db.insert_rows(con, rows)
    db.insert_rows(con, rows)

    count = con.execute("SELECT COUNT(*) FROM parking").fetchone()[0]
    assert count == 1


def test_insert_keeps_distinct_ts_and_floor(tmp_path):
    con = db.connect(tmp_path / "t.db")

    db.insert_rows(con, [
        (1000, "A", 1, 10),
        (1000, "B", 2, 10),
        (1300, "A", 3, 10),
    ])

    count = con.execute("SELECT COUNT(*) FROM parking").fetchone()[0]
    assert count == 3


def test_parse_datetm_accepts_known_formats():
    expected = int(datetime(2026, 8, 24, 13, 5).timestamp())
    assert app.parse_datetm("2026-08-24 13:05") == expected
    assert app.parse_datetm("202608241305") == expected
    assert app.parse_datetm("2026-08-24 13:05:00") == expected
    assert app.parse_datetm("20260824130500") == expected


def test_parse_datetm_rejects_unknown_format():
    with pytest.raises(ValueError):
        app.parse_datetm("24/08/2026 1:05 PM")


def test_parse_rows_handles_bare_item_list():
    payload = {"response": {"body": {"items": [
        {"floor": "  단기주차장 지상  ", "parking": "812", "parkingarea": "1000",
         "datetm": "2026-08-24 13:05"},
    ]}}}

    rows = app.parse_rows(payload)

    assert rows == [(int(datetime(2026, 8, 24, 13, 5).timestamp()),
                     "단기주차장 지상", 812, 1000)]


def test_parse_rows_handles_item_wrapper():
    payload = {"response": {"body": {"items": {"item": [
        {"floor": "장기주차장 P1", "parking": "10", "parkingarea": "20",
         "datetm": "202608241305"},
    ]}}}}

    rows = app.parse_rows(payload)

    assert len(rows) == 1
    assert rows[0][1] == "장기주차장 P1"
    assert rows[0][2:] == (10, 20)
