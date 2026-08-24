import time
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


def test_parse_rows_collapses_one_response_to_a_single_timestamp():
    # 실제 API는 한 응답 안에서도 구역마다 datetm이 몇 초씩 어긋난다. 이를 그대로 쓰면
    # 300초 버킷 경계 근처에서 한 폴이 두 버킷으로 쪼개져 프론트가 부분 합계를 그린다.
    # 응답 전체가 그 응답의 최대 datetm 하나를 공유해야 한다.
    payload = {"response": {"body": {"items": [
        {"floor": "A", "parking": "1", "parkingarea": "10", "datetm": "2026-08-24 13:05:00"},
        {"floor": "B", "parking": "2", "parkingarea": "10", "datetm": "2026-08-24 13:05:07"},
        {"floor": "C", "parking": "3", "parkingarea": "10", "datetm": "2026-08-24 13:05:23"},
    ]}}}

    rows = app.parse_rows(payload)

    expected_ts = int(datetime(2026, 8, 24, 13, 5, 23).timestamp())
    assert {ts for ts, *_ in rows} == {expected_ts}
    assert sorted(floor for _, floor, _, _ in rows) == ["A", "B", "C"]


def test_latest_returns_only_most_recent_snapshot(tmp_path):
    con = db.connect(tmp_path / "t.db")
    now = int(time.time())
    db.insert_rows(con, [
        (now - 300, "A", 10, 100),
        (now - 300, "B", 20, 200),
        (now, "A", 30, 100),
        (now, "B", 40, 200),
    ])

    rows = db.latest(con)

    assert {r["ts"] for r in rows} == {now}
    assert sorted((r["floor"], r["available"]) for r in rows) == [("A", 70), ("B", 160)]


def test_latest_on_empty_db_returns_empty(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert db.latest(con) == []


def test_latest_returns_bare_columns_from_the_max_ts_row(tmp_path):
    # SQLite의 bare-column-follows-MAX(ts) 보장을 신뢰만 하지 말고 증명한다: 같은 층에
    # ts가 다른 두 행을 넣고, parked가 최신 ts의 것인지 확인한다.
    con = db.connect(tmp_path / "t.db")
    now = int(time.time())
    db.insert_rows(con, [
        (now - 1000, "A", 10, 100),
        (now, "A", 99, 100),
    ])

    rows = db.latest(con)

    assert len(rows) == 1
    assert rows[0]["ts"] == now
    assert rows[0]["parked"] == 99


def test_latest_and_series_survive_per_floor_datetm_drift(tmp_path):
    # db.insert_rows 자체는 여전히 층별로 다른 ts를 받아들일 수 있어야 한다 (app.parse_rows가
    # 이제 응답 하나를 하나의 ts로 뭉개지만, db 계층은 그 가정에 기대지 않는다). 세 층 모두
    # 같은 ts를 공유한다고 가정하지 않는다.
    con = db.connect(tmp_path / "t.db")
    base = (int(time.time()) // 300) * 300  # 버킷 경계에 정렬해 series 버킷을 예측 가능하게
    db.insert_rows(con, [
        (base, "A", 10, 100),
        (base + 5, "B", 20, 100),
        (base + 12, "C", 30, 100),
    ])

    latest_rows = db.latest(con)
    assert sorted(r["floor"] for r in latest_rows) == ["A", "B", "C"]

    series_rows = db.series(con, base - 100, base + 100)
    assert {r["ts"] for r in series_rows} == {base}
    assert sorted(r["floor"] for r in series_rows) == ["A", "B", "C"]


def test_latest_excludes_a_floor_stale_beyond_the_freshness_window(tmp_path):
    con = db.connect(tmp_path / "t.db")
    now = int(time.time())
    db.insert_rows(con, [
        (now - db.LATEST_MAX_AGE_SECONDS - 1, "STALE", 10, 100),
        (now, "FRESH", 20, 100),
    ])

    rows = db.latest(con)

    assert [r["floor"] for r in rows] == ["FRESH"]


def test_latest_includes_a_floor_within_the_freshness_window(tmp_path):
    con = db.connect(tmp_path / "t.db")
    now = int(time.time())
    db.insert_rows(con, [(now - db.LATEST_MAX_AGE_SECONDS + 1, "A", 10, 100)])

    rows = db.latest(con)

    assert [r["floor"] for r in rows] == ["A"]


HOUR = 3600
DAY = 86400


def test_series_buckets_by_5_minutes_for_short_range(tmp_path):
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
    ts = int(time.time())  # db.latest() only reports floors seen within the last hour
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


def test_collect_failure_never_raises_or_leaks_key_on_awkward_exceptions(caplog):
    # _log_collect_failure는 collect_loop의 except 안에서 호출된다 — 여기서 예외가 새어
    # 나가면 수집기 태스크가 영구히 죽는다. httpx.DecodingError는 httpx.HTTPError의
    # 서브클래스이면서도 .request가 안 붙은 채로 만들어질 수 있고(재현: 손상된 gzip 응답),
    # httpx.InvalidURL은 아예 httpx.HTTPError가 아니다 — 두 경우 다 예외를 던지지 않고,
    # 어떤 텍스트로도 가짜 키를 새 나가게 하지 않아야 한다.
    import logging

    import httpx

    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)

    awkward = (
        httpx.DecodingError("bad gzip data"),  # HTTPError 서브클래스지만 request 없음
        httpx.InvalidURL("bad url FAKEKEY_DO_NOT_LOG_2"),  # HTTPError조차 아님
        ValueError("boom FAKEKEY_DO_NOT_LOG_3"),  # httpx와 무관한 예외
    )

    for exc in awkward:
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="parking"):
            app._log_collect_failure(exc)  # 던지면 이 자체로 테스트 실패

        assert "FAKEKEY_DO_NOT_LOG" not in caplog.text


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
