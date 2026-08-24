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


def test_parse_datetm_accepts_production_fractional_format():
    expected = int(datetime(2026, 8, 24, 10, 24, 7).timestamp())
    assert app.parse_datetm("20260824102407.000") == expected


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


def test_latest_returns_only_most_recent_snapshot(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [
        (1000, "A", 10, 100),
        (1000, "B", 20, 200),
        (1300, "A", 30, 100),
        (1300, "B", 40, 200),
    ])

    rows = db.latest(con)

    assert {r["ts"] for r in rows} == {1300}
    assert sorted((r["floor"], r["available"]) for r in rows) == [("A", 70), ("B", 160)]


def test_latest_on_empty_db_returns_empty(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert db.latest(con) == []


HOUR = 3600
DAY = 86400


def test_series_returns_raw_points_for_short_range(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [
        (0, "A", 10, 100),
        (300, "A", 20, 100),
        (600, "A", 30, 100),
    ])

    rows = db.series(con, 0, 2 * DAY)

    assert [(r["ts"], r["available"]) for r in rows] == [(0, 90), (300, 80), (600, 70)]


def test_series_buckets_by_hour_for_long_range(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [
        (0, "A", 10, 100),           # 0시 버킷, available 90
        (300, "A", 30, 100),         # 0시 버킷, available 70
        (HOUR + 60, "A", 50, 100),   # 1시 버킷, available 50
    ])

    rows = db.series(con, 0, 10 * DAY)

    assert [(r["ts"], r["available"]) for r in rows] == [(0, 80.0), (HOUR, 50.0)]


def test_series_respects_range_bounds(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(0, "A", 1, 100), (500, "A", 2, 100), (1000, "A", 3, 100)])

    rows = db.series(con, 400, 600)

    assert [r["ts"] for r in rows] == [500]


def test_series_separates_floors(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(0, "A", 10, 100), (0, "B", 20, 100)])

    rows = db.series(con, 0, DAY)

    assert sorted((r["floor"], r["available"]) for r in rows) == [("A", 90), ("B", 80)]


def test_pattern_groups_by_local_weekday_and_hour(tmp_path):
    con = db.connect(tmp_path / "t.db")
    ts = int(datetime(2026, 8, 24, 15, 0).timestamp())   # 로컬 시간대 기준 15시
    db.insert_rows(con, [(ts, "A", 40, 100), (ts + 300, "A", 60, 100)])

    rows = db.pattern(con)

    assert len(rows) == 1
    row = rows[0]
    local = datetime.fromtimestamp(ts)
    assert row["hour"] == local.hour
    assert row["dow"] == int(local.strftime("%w"))
    assert row["available"] == 50.0
    assert row["samples"] == 2


def test_every_known_floor_is_mapped():
    assert app.FLOOR_GROUPS, "FLOOR_GROUPS is empty — run scripts/probe.py first"
    for floor, group in app.FLOOR_GROUPS.items():
        assert app.group_of(floor) == group


def test_unknown_floor_falls_back_to_etc():
    assert app.group_of("존재하지 않는 주차장") == ("기타", "기타")


KNOWN_LIVE_FLOORS = (
    "T1 단기주차장지하1층",
    "T1 단기주차장지하2층",
    "T1 단기주차장지하3층",
    "T1 단기주차장지상층",
    "T1 장기 P1 주차장",
    "T1 장기 P1 주차타워",
    "T1 장기 P2 주차장",
    "T1 장기 P2 주차타워",
    "T1 장기 P3 주차장",
    "T1 P5 예약주차장",
    "T2 단기주차장지하M층",
    "T2 단기주차장지상1층",
    "T2 단기주차장지상2층",
    "T2 단기주차장지상3층",
    "T2 단기주차장지상4층",
    "T2 장기 주차장",
    "T2 P1 장기주차타워",
    "T2 P2 장기주차타워",
    "T2 예약 주차장",
)


def test_every_live_sample_floor_maps_to_a_real_group():
    assert len(KNOWN_LIVE_FLOORS) == 19
    for floor in KNOWN_LIVE_FLOORS:
        assert app.group_of(floor) != ("기타", "기타")


def test_pattern_separates_distinct_hours(tmp_path):
    con = db.connect(tmp_path / "t.db")
    base = int(datetime(2026, 8, 24, 15, 0).timestamp())
    db.insert_rows(con, [(base, "A", 40, 100), (base + 2 * HOUR, "A", 10, 100)])

    rows = db.pattern(con)

    assert len(rows) == 2
    assert sorted(r["hour"] for r in rows) == sorted(
        [datetime.fromtimestamp(base).hour, datetime.fromtimestamp(base + 2 * HOUR).hour]
    )


from fastapi.testclient import TestClient


def test_endpoints_return_grouped_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    con = db.connect(tmp_path / "t.db")
    known_floor = next(iter(app.FLOOR_GROUPS))
    ts = int(datetime(2026, 8, 24, 15, 0).timestamp())
    db.insert_rows(con, [(ts, known_floor, 40, 100)])
    con.close()

    with TestClient(app.app) as client:
        current = client.get("/api/current").json()
        assert current[0]["floor"] == known_floor
        assert current[0]["available"] == 60
        assert current[0]["terminal"] == app.FLOOR_GROUPS[known_floor][0]
        assert current[0]["kind"] == app.FLOOR_GROUPS[known_floor][1]

        pattern = client.get("/api/pattern").json()
        assert pattern[0]["samples"] == 1


def test_series_endpoint_windows_from_now(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    con = db.connect(tmp_path / "t.db")
    known_floor = next(iter(app.FLOOR_GROUPS))
    now = int(time.time())
    db.insert_rows(con, [
        (now - 600, known_floor, 40, 100),        # 창 안
        (now - 5 * DAY, known_floor, 10, 100),    # 창 밖
    ])
    con.close()

    with TestClient(app.app) as client:
        rows = client.get("/api/series?days=1").json()

    assert [r["ts"] for r in rows] == [now - 600]
