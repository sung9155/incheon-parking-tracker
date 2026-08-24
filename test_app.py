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


def test_latest_returns_bare_columns_from_the_max_ts_row(tmp_path):
    # SQLite의 bare-column-follows-MAX(ts) 보장을 신뢰만 하지 말고 증명한다: 같은 층에
    # ts가 다른 두 행을 넣고, parked가 최신 ts의 것인지 확인한다.
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [
        (1000, "A", 10, 100),
        (2000, "A", 99, 100),
    ])

    rows = db.latest(con)

    assert len(rows) == 1
    assert rows[0]["ts"] == 2000
    assert rows[0]["parked"] == 99


def test_latest_and_series_survive_per_floor_datetm_drift(tmp_path):
    # 실제 API는 한 번의 폴에서도 층마다 datetm이 몇 초씩 어긋난다 (live-sample.json 재현:
    # 한 폴에 datetm 14종, 23초 폭). 세 층 모두 같은 ts를 공유한다고 가정하지 않는다.
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [
        (1000, "A", 10, 100),
        (1005, "B", 20, 100),
        (1012, "C", 30, 100),
    ])

    latest_rows = db.latest(con)
    assert sorted(r["floor"] for r in latest_rows) == ["A", "B", "C"]

    series_rows = db.series(con, 900, 1100)
    assert {r["ts"] for r in series_rows} == {900}
    assert sorted(r["floor"] for r in series_rows) == ["A", "B", "C"]


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

    # 300초 버킷: 500 -> (500 // 300) * 300 == 300. 범위 필터링(0, 1000 제외)이지
    # 정렬/버킷 위치는 그대로 검증한다.
    assert [r["ts"] for r in rows] == [300]


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

    # days=1은 짧은 범위라 300초 버킷을 탄다 — (now - 600)을 그대로가 아니라
    # 버킷 경계로 반올림한 값과 비교한다.
    bucket = db.SHORT_BUCKET_SECONDS
    assert [r["ts"] for r in rows] == [((now - 600) // bucket) * bucket]


def test_collect_failure_never_logs_the_service_key(caplog):
    import logging

    import httpx

    request = httpx.Request(
        "GET", "https://apis.data.go.kr/x",
        params={"serviceKey": "FAKEKEY_DO_NOT_LOG", "type": "json"},
    )
    response = httpx.Response(403, request=request)
    exc = httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

    with caplog.at_level(logging.ERROR, logger="parking"):
        app._log_collect_failure(exc)

    assert "FAKEKEY_DO_NOT_LOG" not in caplog.text
    assert "403" in caplog.text


def test_lifespan_cancels_collector_task_cleanly_on_shutdown(monkeypatch):
    import time

    monkeypatch.setenv("COLLECT", "1")
    monkeypatch.setenv("SERVICE_KEY", "unused-in-this-test")

    calls = []

    async def fake_collect_once(client, con, key):
        calls.append(1)
        return 0

    monkeypatch.setattr(app, "collect_once", fake_collect_once)

    # 실패 시 TestClient __exit__가 (task.cancel() 뒤 await task에서) 예외를 흘려보내
    # 테스트가 그 자체로 실패한다 — COLLECT=0인 다른 테스트들은 이 종료 경로를 건드리지 않는다.
    with TestClient(app.app):
        time.sleep(0.05)  # 수집 루프가 한 틱 돌 시간을 준다

    assert calls  # 루프가 실제로 시작됐다 (COLLECT=1이 무시되지 않았다)
