import pathlib
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
    db.insert_rows(con, [(now - 60, "A", 10, 100)])

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
    assert app.FLOOR_GROUPS, "FLOOR_GROUPS is empty"
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


def test_result_msg_extracts_message_or_falls_back_safely():
    # 일일 쿼터 소진 시 data.go.kr은 HTTP 200 + 에러 바디를 주고, resultMsg에 원인이
    # 담긴다. 그 필드가 없거나 payload 형태가 예상과 다를 때도 절대 예외를 던지지 않는다
    # (collect_once의 except 경로 안에서 호출되므로).
    quota_exceeded = {"response": {"header": {"resultMsg": "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"}}}
    assert app._result_msg(quota_exceeded) == "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"
    assert app._result_msg({}) == "no resultMsg"
    assert app._result_msg(None) == "no resultMsg"
    assert app._result_msg("not a dict") == "no resultMsg"


def test_lifespan_cancels_collector_task_cleanly_on_shutdown(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("COLLECT", "1")
    monkeypatch.setenv("SERVICE_KEY", "unused-in-this-test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

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


def test_lifespan_fails_loudly_on_empty_service_key(tmp_path, monkeypatch):
    # compose.yml의 매핑 형식(SERVICE_KEY: ${SERVICE_KEY})은 변수가 unset이어도 빈
    # 문자열로 치환되므로 os.environ["SERVICE_KEY"]는 절대 KeyError를 던지지 않는다.
    # 빈 값 자체를 거부해야 컨테이너가 조용히 403을 반복하는 대신 바로 죽는다.
    monkeypatch.setenv("COLLECT", "1")
    monkeypatch.setenv("SERVICE_KEY", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with pytest.raises(RuntimeError, match="SERVICE_KEY"):
        with TestClient(app.app):
            pass


def test_startup_makes_collect_success_visible(tmp_path, monkeypatch, capfd):
    """기동 경로가 끝나면 "collected N rows"가 실제로 로그에 나와야 한다.

    uvicorn은 dictConfig로 자기 로거만 설정하고 root에는 핸들러를 두지 않아서,
    아무것도 안 하면 log.error만 lastResort로 새어나가고 log.info는 통째로 버려진다.
    무인 운영에서 수집 성공을 확인할 유일한 신호가 사라진다.

    헬퍼를 직접 부르지 않고 lifespan을 통과시킨다 — 그래야 배선이 빠졌을 때 잡힌다.
    """
    import logging
    import logging.config

    import uvicorn.config

    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    saved_handlers = list(app.log.handlers)
    saved_level, saved_propagate = app.log.level, app.log.propagate
    # 갓 기동한 프로세스와 같은 상태로 되돌려야 전제 자체를 검증할 수 있다.
    app.log.handlers.clear()
    app.log.setLevel(logging.NOTSET)
    app.log.propagate = True
    try:
        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
        assert app.log.getEffectiveLevel() > logging.INFO, "전제: 설정 전에는 INFO가 막혀 있다"

        with TestClient(app.app):
            app.log.info("collected %d rows", 19)

        assert "collected 19 rows" in capfd.readouterr().err
    finally:
        app.log.handlers[:] = saved_handlers
        app.log.level, app.log.propagate = saved_level, saved_propagate


# ---------------------------------------------------------------- 날짜 범위 조회

def test_day_range_epoch_covers_the_whole_local_day():
    start, end = app.day_range_epoch("2026-09-24", "2026-09-24")

    assert datetime.fromtimestamp(start) == datetime(2026, 9, 24, 0, 0, 0)
    assert datetime.fromtimestamp(end) == datetime(2026, 9, 24, 23, 59, 59)


def test_day_range_epoch_spans_multiple_days():
    start, end = app.day_range_epoch("2026-09-24", "2026-09-27")

    assert datetime.fromtimestamp(start) == datetime(2026, 9, 24, 0, 0, 0)
    assert datetime.fromtimestamp(end) == datetime(2026, 9, 27, 23, 59, 59)


def test_series_endpoint_honours_an_explicit_date_range(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    con = db.connect(tmp_path / "t.db")
    inside = int(datetime(2026, 9, 24, 12, 0).timestamp())
    before = int(datetime(2026, 9, 23, 12, 0).timestamp())
    after = int(datetime(2026, 9, 25, 12, 0).timestamp())
    db.insert_rows(con, [
        (before, "A", 10, 100),
        (inside, "A", 20, 100),
        (after, "A", 30, 100),
    ])
    con.close()

    with TestClient(app.app) as client:
        rows = client.get("/api/series?from=2026-09-24&to=2026-09-24").json()

    size = db.auto_bucket(*app.day_range_epoch("2026-09-24", "2026-09-24"))
    bucket = (inside // size) * size
    assert [r["ts"] for r in rows] == [bucket]


# ------------------------------------------------------------------- 황금연휴

def test_golden_holidays_finds_chuseok_2026():
    # 2026 추석: 9/24(목) 전날, 9/25(금) 추석, 9/26(토) 다음날, 9/27(일) -> 4일
    runs = app.golden_holidays("2026-09-01", "2026-09-30")

    chuseok = [r for r in runs if r["start"] == "2026-09-24"]
    assert len(chuseok) == 1
    assert chuseok[0] == {"start": "2026-09-24", "end": "2026-09-27",
                          "name": "추석", "days": 4}


def test_golden_holidays_finds_lunar_new_year_2026():
    # 2/14(토) 2/15(일) + 설날 2/16~2/18 -> 5일. 음력 기반이라 계산으로는 못 구한다.
    runs = app.golden_holidays("2026-02-01", "2026-02-28")

    seollal = [r for r in runs if r["name"] == "설날"]
    assert len(seollal) == 1
    assert seollal[0]["start"] == "2026-02-14"
    assert seollal[0]["end"] == "2026-02-18"
    assert seollal[0]["days"] == 5


def test_golden_holidays_ignores_constitution_day():
    # 제헌절(7/17)은 2008년부터 공휴일이 아니다. holidays 패키지는 아직 포함하므로
    # 걸러내야 한다 — 2026년에는 금요일이라 그대로 두면 7/17~7/19가 가짜 3일 연휴가 된다.
    runs = app.golden_holidays("2026-07-01", "2026-07-31")

    assert runs == []


def test_golden_holidays_ignores_a_plain_weekend():
    runs = app.golden_holidays("2026-07-11", "2026-07-12")   # 토·일뿐

    assert runs == []


def test_golden_holidays_returns_a_run_overlapping_the_window_whole():
    # 창을 연휴 한복판으로 잘라도 구간 전체를 돌려줘야 음영이 잘리지 않는다.
    runs = app.golden_holidays("2026-09-25", "2026-09-25")

    assert [r["start"] for r in runs] == ["2026-09-24"]
    assert [r["end"] for r in runs] == ["2026-09-27"]


def test_holidays_endpoint_returns_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with TestClient(app.app) as client:
        runs = client.get("/api/holidays?from=2026-09-01&to=2026-09-30").json()

    assert {"start": "2026-09-24", "end": "2026-09-27", "name": "추석", "days": 4} in runs


def test_golden_holidays_ignores_constitution_day_substitute():
    # 제헌절이 토요일인 해에는 월요일에 "제헌절 대체 휴일"이 붙는다. 정확 일치로만
    # 거르면 이게 빠져나가 토·일·월 3일짜리 가짜 연휴가 된다. 2027년이 그런 해다.
    runs = app.golden_holidays("2027-07-01", "2027-07-31")

    assert runs == []


def test_series_reports_capacity_so_occupancy_can_be_computed(tmp_path):
    # 사용률은 (capacity - available) / capacity 로 계산한다. capacity가 없으면
    # 프론트에서 사용률을 낼 방법이 없다. 층별 capacity는 상수이므로 AVG는 그 값 그대로다.
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(0, "A", 40, 100), (300, "A", 60, 100)])

    rows = db.series(con, 0, DAY)

    assert [r["capacity"] for r in rows] == [100, 100]
    assert [r["available"] for r in rows] == [60, 40]


def test_pattern_reports_capacity_too(tmp_path):
    con = db.connect(tmp_path / "t.db")
    ts = int(datetime(2026, 8, 24, 15, 0).timestamp())
    db.insert_rows(con, [(ts, "A", 40, 100)])

    rows = db.pattern(con)

    assert rows[0]["capacity"] == 100


def test_layout_diagram_covers_every_mapped_floor():
    """배치도의 LAYOUT이 FLOOR_GROUPS와 정확히 일치해야 한다.

    공항이 구역을 추가하면 group_of는 "기타"로 흘려보내며 경고를 남기지만, 배치도는
    조용히 빠뜨린다 — 화면에 없는 주차장이 생기는 셈이라 이 테스트로 잡는다.
    """
    import re

    html = pathlib.Path("static/index.html").read_text(encoding="utf-8")
    block = html[html.index("const LAYOUT = ["):html.index("function renderLayout")]
    listed = re.findall(r"'(T[12] [^']+)'", block)

    assert len(listed) == len(set(listed)), "배치도에 중복된 구역이 있다"
    assert set(listed) == set(app.FLOOR_GROUPS)


# ------------------------------------------------------- 조회 단위(버킷) 선택

def test_auto_bucket_picks_the_finest_that_fits():
    # 5분 수집이므로 하루는 288포인트 — 그대로 5분 단위로 볼 수 있다.
    assert db.auto_bucket(0, DAY) == 300
    # 7일을 5분으로 보면 2016포인트라 과하다. 10분(1008)이면 들어간다.
    assert db.auto_bucket(0, 7 * DAY) == 600
    # 30일은 30분(1440)까지 내려간다. 예전처럼 곧장 1시간으로 뭉개지 않는다.
    assert db.auto_bucket(0, 30 * DAY) == 1800


def test_auto_bucket_never_returns_a_bucket_that_blows_the_budget():
    for days in (1, 3, 7, 30, 90, 365, 1000):
        span = days * DAY
        assert span / db.auto_bucket(0, span) <= db.TARGET_POINTS


def test_series_honours_an_explicit_bucket(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [
        (0, "A", 10, 100),      # 30분 버킷 0
        (300, "A", 30, 100),    # 30분 버킷 0
        (1800, "A", 50, 100),   # 30분 버킷 1800
    ])

    rows = db.series(con, 0, DAY, bucket=1800)

    assert [(r["ts"], r["available"]) for r in rows] == [(0, 80.0), (1800, 50.0)]


def test_explicit_bucket_is_honoured_even_past_the_auto_target(tmp_path):
    # 자동은 7일에 10분을 고르지만, 사람이 5분을 고르면 그대로 5분이어야 한다.
    # 직접 고른 해상도를 조용히 내려버리면 고른 의미가 없다.
    assert db.auto_bucket(0, 7 * DAY) == 600
    assert db.clamp_bucket(0, 7 * DAY, 300) == 300


def test_series_clamps_a_bucket_that_would_return_too_many_points(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(0, "A", 10, 100)])

    # 1년을 5분 단위로 달라는 요청은 10만 포인트가 넘는다. 조용히 굵은 단위로 내린다.
    span = 365 * DAY
    used = db.clamp_bucket(0, span, 300)

    assert used > 300
    assert span / used <= db.MAX_POINTS


def test_series_endpoint_accepts_a_bucket_parameter(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    con = db.connect(tmp_path / "t.db")
    base = int(datetime(2026, 9, 24, 0, 0).timestamp())
    db.insert_rows(con, [(base, "A", 10, 100), (base + 600, "A", 30, 100)])
    con.close()

    with TestClient(app.app) as client:
        half = client.get("/api/series?from=2026-09-24&to=2026-09-24&bucket=1800").json()
        fine = client.get("/api/series?from=2026-09-24&to=2026-09-24&bucket=300").json()

    assert len(half) == 1          # 두 관측이 같은 30분 버킷에 들어간다
    assert len(fine) == 2          # 5분 단위로는 따로 떨어진다


# ------------------------------------------- 패턴: 조회 창 제한 + 연휴 제외

def test_pattern_can_be_bounded_to_a_recent_window(tmp_path):
    # 전체 스캔은 이력이 쌓일수록 느려진다. ts 하한을 주면 PK 범위 스캔으로 끝난다.
    con = db.connect(tmp_path / "t.db")
    now = int(time.time())
    old = now - 400 * DAY
    db.insert_rows(con, [(old, "A", 90, 100), (now, "A", 10, 100)])

    rows = db.pattern(con, since=now - 180 * DAY)

    assert len(rows) == 1
    assert rows[0]["available"] == 90        # 최근 행(10대 주차)만 남는다


def test_pattern_excludes_given_days(tmp_path):
    # "평소"에 연휴가 섞이면 기준선이 올라간다. 연휴 날짜는 빼야 진짜 평소가 된다.
    con = db.connect(tmp_path / "t.db")
    normal = int(datetime(2026, 9, 17, 15, 0).timestamp())     # 목요일
    holiday = int(datetime(2026, 9, 24, 15, 0).timestamp())    # 추석 목요일
    db.insert_rows(con, [(normal, "A", 20, 100), (holiday, "A", 95, 100)])

    both = db.pattern(con)
    without = db.pattern(con, exclude_days={"2026-09-24"})

    assert len(both) == 1 and both[0]["samples"] == 2
    assert len(without) == 1 and without[0]["samples"] == 1
    assert without[0]["available"] == 80        # 연휴(5자리)가 빠져 평소치만 남는다


def test_pattern_window_uses_the_primary_key_range(tmp_path):
    con = db.connect(tmp_path / "t.db")
    plan = con.execute(
        "EXPLAIN QUERY PLAN " + db.pattern_sql(since=1, exclude_days=set()), (1,)
    ).fetchall()

    assert any("USING PRIMARY KEY" in " ".join(str(c) for c in row) for row in plan), plan


# --------------------------------------------------------------- 헬스체크

def test_health_reports_ok_when_collection_is_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(int(time.time()) - 60, "A", 10, 100)])
    con.close()

    with TestClient(app.app) as client:
        r = client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["age_seconds"] < 300
    assert body["floors"] == 1
    assert body["rows"] == 1


def test_health_reports_stale_with_a_failing_status_code(tmp_path, monkeypatch):
    # 수집기가 죽으면 모니터링이 걸 수 있는 신호가 있어야 한다. 200을 돌려주면
    # Uptime Kuma 같은 도구는 정상으로 읽는다.
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(int(time.time()) - 4 * 3600, "A", 10, 100)])
    con.close()

    with TestClient(app.app) as client:
        r = client.get("/api/health")

    assert r.status_code == 503
    assert r.json()["status"] == "stale"


def test_health_on_an_empty_database_is_stale_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with TestClient(app.app) as client:
        r = client.get("/api/health")

    assert r.status_code == 503
    assert r.json()["last_collect"] is None


# ------------------------------------------------------------------ CSV

def test_csv_export_returns_raw_rows(tmp_path, monkeypatch):
    # 내보내기는 버킷 평균이 아니라 원본 행이어야 한다 — 백업 용도이기 때문이다.
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    base = int(datetime(2026, 9, 24, 0, 5).timestamp())
    db.insert_rows(con, [(base, "A", 10, 100), (base + 300, "A", 20, 100)])
    con.close()

    with TestClient(app.app) as client:
        r = client.get("/api/export.csv?from=2026-09-24&to=2026-09-24")

    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == "datetime,ts,floor,parked,capacity,available"
    assert len(lines) == 3
    assert lines[1].startswith("2026-09-24 00:05:00,")
    assert lines[1].endswith(",A,10,100,90")


def test_series_reports_the_bucket_it_used(tmp_path, monkeypatch):
    # 프론트는 이 값으로 x축을 채운다 — 없으면 결측 구간을 직선으로 이어 그린다.
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with TestClient(app.app) as client:
        one_day = client.get("/api/series?from=2026-09-24&to=2026-09-24")
        a_week = client.get("/api/series?from=2026-09-24&to=2026-09-30")
        forced = client.get("/api/series?from=2026-09-24&to=2026-09-30&bucket=1800")

    assert one_day.headers["X-Bucket-Seconds"] == "300"
    assert a_week.headers["X-Bucket-Seconds"] == "600"     # 자동이 10분을 고른다
    assert forced.headers["X-Bucket-Seconds"] == "1800"
