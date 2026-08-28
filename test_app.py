import pathlib
import time
from datetime import date, datetime

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

    # 주차만 심었으므로 승객 예고는 stale이고, 따라서 전체도 stale이다.
    assert r.status_code == 503
    body = r.json()
    assert body["sources"]["parking"]["status"] == "ok"
    assert body["sources"]["parking"]["age_seconds"] < 300
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


# --------------------------------------------------- 수집 소스 여러 개 다루기

def test_sources_are_declared_with_name_interval_and_collector():
    # 소스를 늘리는 일이 "리스트에 한 줄 추가"여야 한다. 루프를 복제하기 시작하면
    # 재시도 정책·로깅·종료 처리가 소스마다 갈라진다.
    assert [s.name for s in app.SOURCES] == ["parking", "passengers", "congestion", "fees", "spaces"]
    for src in app.SOURCES:
        assert src.interval > 0
        assert callable(src.collect)
        assert callable(src.last_ts)
    # 혼잡도는 1~2분마다 갱신되므로 주차보다 촘촘히 받는다.
    by_name = {s.name: s for s in app.SOURCES}
    assert by_name["congestion"].interval < by_name["parking"].interval


def test_lifespan_starts_one_task_per_source(tmp_path, monkeypatch):
    import asyncio as aio

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SERVICE_KEY", "dummy")
    monkeypatch.delenv("COLLECT", raising=False)

    started = []

    async def never_ending(client, con, key):
        started.append(True)
        await aio.sleep(3600)

    extra = app.Source("fake", 60, never_ending, lambda con: None)
    monkeypatch.setattr(app, "SOURCES", [app.SOURCES[0]._replace(collect=never_ending), extra])

    with TestClient(app.app):
        pass                       # 기동했다가 바로 종료 — 태스크가 깨끗이 정리돼야 한다

    assert len(started) == 2


def test_health_reports_each_source_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(int(time.time()) - 60, "A", 10, 100)])
    con.close()

    with TestClient(app.app) as client:
        body = client.get("/api/health").json()

    assert set(body["sources"]) == {"parking", "passengers", "congestion", "fees", "spaces"}
    assert body["sources"]["parking"]["status"] == "ok"
    assert body["sources"]["parking"]["age_seconds"] < 300
    # 승객 예고는 한 번도 수집된 적이 없다 -> 그 소스는 stale이고,
    # 하나라도 stale이면 전체가 stale이어야 한다.
    assert body["sources"]["passengers"]["status"] == "stale"
    assert body["status"] == "stale"


def test_one_failing_source_does_not_stop_the_others(tmp_path, monkeypatch):
    # 소스를 나눈 이유가 이것이다. 승객예고 API가 죽어도 주차 수집은 계속돼야 한다.
    import asyncio as aio

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SERVICE_KEY", "dummy")
    monkeypatch.delenv("COLLECT", raising=False)

    healthy_ticks = []

    async def always_fails(client, con, key):
        raise RuntimeError("this source is broken")

    async def keeps_working(client, con, key):
        healthy_ticks.append(True)
        await aio.sleep(0)
        return 19

    broken = app.Source("broken", 0, always_fails, lambda con: None)
    healthy = app.Source("healthy", 0, keeps_working, lambda con: None)
    monkeypatch.setattr(app, "SOURCES", [broken, healthy])

    async def drive():
        con = db.connect(tmp_path / "t.db")
        tasks = [aio.create_task(app.collect_loop(s, con, "k")) for s in (broken, healthy)]
        await aio.sleep(0.05)                 # 두 루프가 여러 틱 돌 시간
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except aio.CancelledError:
                pass
        con.close()

    aio.run(drive())

    # 망가진 소스가 예외를 계속 던져도 멀쩡한 소스는 계속 돌아야 한다
    assert len(healthy_ticks) > 1, healthy_ticks


# ------------------------------------------------------------- 승객 예고

PASSENGER_SAMPLE = {"response": {"body": {"items": [
    {"adate": "20260825", "atime": "00_01",
     "t1eg1": "818.0", "t1eg2": "0.0", "t1eg3": "576.0", "t1eg4": "0.0", "t1egsum1": "1394.0",
     "t1dg1": "0.0", "t1dg2": "706.0", "t1dg3": "0.0", "t1dg4": "0.0", "t1dg5": "0.0",
     "t1dg6": "0.0", "t1dgsum1": "706.0",
     "t2eg1": "0.0", "t2eg2": "0.0", "t2egsum1": "0.0",
     "t2dg1": "0.0", "t2dg2": "0.0", "t2dgsum2": "0.0", "tmp1": "", "tmp2": ""},
    # 합계 행 — 시간대가 아니므로 저장하면 이중 계산이 된다
    {"adate": "합계", "atime": "합계",
     "t1eg1": "1.0", "t1eg2": "0.0", "t1eg3": "0.0", "t1eg4": "0.0", "t1egsum1": "1.0",
     "t1dg1": "0.0", "t1dg2": "0.0", "t1dg3": "0.0", "t1dg4": "0.0", "t1dg5": "0.0",
     "t1dg6": "0.0", "t1dgsum1": "0.0",
     "t2eg1": "0.0", "t2eg2": "0.0", "t2egsum1": "0.0",
     "t2dg1": "0.0", "t2dg2": "0.0", "t2dgsum2": "0.0", "tmp1": "", "tmp2": ""},
]}}}


def test_passenger_gate_labels_follow_the_official_guide():
    # 활용가이드 V5.0 기준. t1eg1~4는 번호가 아니라 구역 이름이다 — 번호로 저장하면
    # 화면의 "입국장 2번"이 실제로는 E·F 구역을 가리키게 되어 공항 안내판과 어긋난다.
    assert app.PASSENGER_FIELDS["t1eg1"] == ("T1", "입국", "A·B")
    assert app.PASSENGER_FIELDS["t1eg2"] == ("T1", "입국", "E·F")
    assert app.PASSENGER_FIELDS["t1eg3"] == ("T1", "입국", "C")
    assert app.PASSENGER_FIELDS["t1eg4"] == ("T1", "입국", "D")
    assert app.PASSENGER_FIELDS["t2eg1"] == ("T2", "입국", "A")
    assert app.PASSENGER_FIELDS["t2eg2"] == ("T2", "입국", "B")
    assert app.PASSENGER_FIELDS["t1dg6"] == ("T1", "출국", "6")
    # 합계 필드는 매핑에 없어야 한다 — 게이트에서 더하면 나오는 값이고, 둘 다
    # 저장하면 서로 어긋날 수 있다.
    for summed in ("t1egsum1", "t1dgsum1", "t2egsum1", "t2dgsum2"):
        assert summed not in app.PASSENGER_FIELDS


def test_parse_passengers_skips_the_total_row():
    rows = app.parse_passengers(PASSENGER_SAMPLE)

    assert all(r[0] == "2026-08-25" for r in rows)
    assert {r[1] for r in rows} == {0}                    # '00_01' -> 0시
    assert len(rows) == len(app.PASSENGER_FIELDS)         # 게이트마다 한 행


def test_parse_passengers_maps_values_to_the_right_gate():
    by_gate = {(t, d, g): n for _, _, t, d, g, n in app.parse_passengers(PASSENGER_SAMPLE)}

    assert by_gate[("T1", "입국", "A·B")] == 818
    assert by_gate[("T1", "입국", "C")] == 576
    assert by_gate[("T1", "출국", "2")] == 706
    assert by_gate[("T2", "출국", "1")] == 0


def test_parse_passengers_totals_match_the_apis_own_sums():
    # 게이트 합이 API가 준 합계와 같아야 한다. 어긋나면 매핑이 틀린 것이다.
    item = PASSENGER_SAMPLE["response"]["body"]["items"][0]
    rows = app.parse_passengers(PASSENGER_SAMPLE)
    t1_dep = sum(n for *_, t, d, g, n in [(None, None, *r[2:]) for r in rows]
                 if t == "T1" and d == "출국")

    assert t1_dep == int(float(item["t1dgsum1"]))


def test_passengers_endpoint_returns_the_range(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    db.upsert_passengers(con, [
        ("2026-08-25", 9, "T1", "출국", "1", 500),
        ("2026-08-25", 9, "T1", "출국", "2", 300),
        ("2026-08-25", 9, "T1", "입국", "C", 200),
        ("2026-08-27", 9, "T1", "출국", "1", 999),      # 구간 밖
    ], 1)
    con.close()

    with TestClient(app.app) as client:
        rows = client.get("/api/passengers?from=2026-08-25&to=2026-08-26").json()

    assert {r["adate"] for r in rows} == {"2026-08-25"}
    dep = [r for r in rows if r["direction"] == "출국"]
    assert sum(r["expected"] for r in dep) == 800


def test_upsert_replaces_a_revised_forecast(tmp_path):
    # 예고는 갱신된다. 주차와 달리 최신 값이 이겨야 한다.
    con = db.connect(tmp_path / "t.db")
    key = ("2026-08-25", 9, "T1", "출국", "1")
    db.upsert_passengers(con, [(*key, 500)], 100)
    db.upsert_passengers(con, [(*key, 620)], 200)

    rows = db.passengers(con, "2026-08-25", "2026-08-25")

    assert len(rows) == 1
    assert rows[0]["expected"] == 620


# ------------------------------------------------------- 출국장 혼잡도

CONGESTION_T1 = {"response": {"body": {"items": [
    {"gateId": "DG3_E", "terminalId": "P01", "waitTime": "33", "waitLength": "155",
     "occurtime": "20260825101000", "operatingTime": "00:00~24:00"},
    {"gateId": "DG1_E", "terminalId": "P01", "waitTime": "6", "waitLength": "0",
     "occurtime": "20260825101000", "operatingTime": ""},      # 미운영
    {"gateId": "DG5_W", "terminalId": "P01", "waitTime": "60+", "waitLength": "900",
     "occurtime": "20260825101000", "operatingTime": "05:00~22:00"},
]}}}


def test_parse_congestion_reads_gate_wait_and_queue():
    rows = app.parse_congestion(CONGESTION_T1)
    by_gate = {r[2]: r for r in rows}

    ts, terminal, gate, minutes, people, capped, operating = by_gate["DG3_E"]
    assert terminal == "T1"
    assert minutes == 33 and people == 155 and capped == 0
    assert datetime.fromtimestamp(ts) == datetime(2026, 8, 25, 10, 10, 0)
    assert operating == "00:00~24:00"


def test_parse_congestion_marks_the_60_plus_cap():
    # 문서상 60분을 넘으면 '60+'로만 온다. 60으로 저장하되 잘렸다는 표시를 남긴다 —
    # 그러지 않으면 62분과 3시간이 화면에서 똑같아 보인다.
    row = {r[2]: r for r in app.parse_congestion(CONGESTION_T1)}["DG5_W"]
    assert row[3] == 60 and row[5] == 1


def test_parse_congestion_flags_a_closed_gate():
    # operatingTime이 비면 그 시각에 운영하지 않는 출국장이다. 0명을 '한산하다'로
    # 읽으면 닫힌 곳으로 사람을 보내게 된다.
    row = {r[2]: r for r in app.parse_congestion(CONGESTION_T1)}["DG1_E"]
    assert row[6] == ""


def test_terminal_ids_map_to_the_names_used_everywhere_else():
    # 주차·승객예고는 T1/T2를 쓴다. 혼잡도만 P01/P03이면 화면에서 못 합친다.
    assert app.TERMINAL_IDS == {"P01": "T1", "P03": "T2"}


def test_congestion_round_trips_through_the_database(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_congestion(con, app.parse_congestion(CONGESTION_T1))

    rows = db.congestion_latest(con)

    assert {r["gate"] for r in rows} == {"DG3_E", "DG1_E", "DG5_W"}
    busiest = max(rows, key=lambda r: r["wait_people"])
    assert busiest["gate"] == "DG5_W" and busiest["wait_capped"] == 1


def test_congestion_endpoint_returns_latest_per_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    db.upsert_congestion(con, [
        (1000, "T1", "DG3_E", 20, 100, 0, "00:00~24:00"),
        (1300, "T1", "DG3_E", 35, 170, 0, "00:00~24:00"),   # 같은 게이트의 더 최근 측정
        (1300, "T2", "DG1_A", 9, 314, 0, "00:00~24:00"),
    ])
    con.close()

    with TestClient(app.app) as client:
        rows = client.get("/api/congestion").json()

    by_gate = {(r["terminal"], r["gate"]): r for r in rows}
    assert len(rows) == 2
    assert by_gate[("T1", "DG3_E")]["wait_minutes"] == 35     # 옛 측정이 이기면 안 된다
    assert by_gate[("T2", "DG1_A")]["wait_people"] == 314


def test_congestion_level_follows_the_official_thresholds():
    # 가이드 별첨: 20분 미만 원활 / 20~40 보통 / 40~60 혼잡 / 60분 이상 매우혼잡
    assert app.congestion_level(0) == "원활"
    assert app.congestion_level(19) == "원활"
    assert app.congestion_level(20) == "보통"
    assert app.congestion_level(39) == "보통"
    assert app.congestion_level(40) == "혼잡"
    assert app.congestion_level(59) == "혼잡"
    assert app.congestion_level(60) == "매우혼잡"


# ------------------------------------------- 혼잡도 추이 (버킷 평균)

def test_congestion_series_buckets_to_the_given_size(tmp_path):
    # 주차 차트와 같은 x축(같은 버킷)에 얹어야 십자선 동기화가 성립한다. 3분 수집을
    # 5분 버킷으로 평균 내면 한 버킷에 1~2개 측정이 들어간다.
    con = db.connect(tmp_path / "t.db")
    db.upsert_congestion(con, [
        (0,   "T1", "DG3_E", 30, 100, 0, "00:00~24:00"),
        (180, "T1", "DG3_E", 40, 120, 0, "00:00~24:00"),   # 같은 300초 버킷
        (300, "T1", "DG3_E", 60, 200, 1, "00:00~24:00"),   # 다음 버킷, 60+ 측정
    ])

    rows = db.congestion_series(con, 0, 900, bucket=300)

    assert [(r["ts"], r["wait_minutes"], r["wait_capped"]) for r in rows] == [
        (0, 35.0, 0), (300, 60.0, 1)]


def test_congestion_series_endpoint_returns_bucketed_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    base = int(datetime(2026, 8, 25, 9, 0).timestamp())
    db.upsert_congestion(con, [
        (base,       "T1", "DG3_E", 30, 100, 0, "00:00~24:00"),
        (base + 180, "T1", "DG3_E", 40, 120, 0, "00:00~24:00"),
    ])
    con.close()

    with TestClient(app.app) as client:
        rows = client.get(
            "/api/congestion/series?from=2026-08-25&to=2026-08-25&bucket=300").json()

    assert len(rows) == 1
    assert rows[0]["wait_minutes"] == 35.0
    assert rows[0]["gate"] == "DG3_E"


# ------------------------------------------------------- 셔틀 시간표

SHUTTLE_SAMPLE = {"response": {"body": {"items": [
    {"stopId": "10000200", "routeId": "11100001", "dayType": "1", "oprOrd": "1",
     "staOrd": "3", "startTime": "458"},           # 자릿수 안 채워진 04:58
    {"stopId": "10000200", "routeId": "11100006", "dayType": "1", "oprOrd": "2",
     "staOrd": "3", "startTime": "0510"},
    {"stopId": "10000180", "routeId": "11100001", "dayType": "1", "oprOrd": "9",
     "staOrd": "1", "startTime": "2415"},           # 24:15 = 익일 00:15 표기
    {"stopId": "10000020", "routeId": "11100001", "dayType": "1", "oprOrd": "5",
     "staOrd": "1", "startTime": "0700"},           # AICC차고지 — 터미널 아님
    {"stopId": "3918942261", "routeId": "1000000000", "dayType": "1", "oprOrd": "0",
     "staOrd": "0", "startTime": "9999"},           # 실데이터에 섞여 있는 쓰레기 행
]}}}


def test_shuttle_timetable_keeps_only_terminal_stops_sorted_numerically():
    stops = app.shuttle_timetable(SHUTTLE_SAMPLE)
    by_id = {s["stop_id"]: s for s in stops}

    assert set(by_id) == {"10000200", "10000180"}          # 차고지·쓰레기 행 제외
    assert by_id["10000200"]["terminal"] == "T1"
    assert by_id["10000180"]["terminal"] == "T2"
    # '458'이 '0510'보다 먼저 — 문자열 정렬이면 뒤집힌다
    assert by_id["10000200"]["times"] == ["04:58", "05:10"]
    assert by_id["10000180"]["times"] == ["24:15"]          # 익일 표기는 그대로 보여준다


def test_shuttle_day_type_uses_the_holiday_calendar():
    # 주말과 공휴일이 2(휴일). 설날 같은 음력 공휴일도 잡혀야 한다.
    assert app.shuttle_day_type(date(2026, 8, 25)) == 1     # 화요일
    assert app.shuttle_day_type(date(2026, 8, 29)) == 2     # 토요일
    assert app.shuttle_day_type(date(2026, 2, 17)) == 2     # 설날 (화요일)


def test_shuttle_endpoint_serves_todays_day_type(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    async def fake_fetch(day_type):
        return [{"stop_id": "10000200", "terminal": "T1",
                 "name": "제1여객터미널(동)", "times": ["04:58"], "_dt": day_type}]

    monkeypatch.setattr(app, "fetch_shuttle", fake_fetch)

    with TestClient(app.app) as client:
        body = client.get("/api/shuttle").json()

    assert body["day_type"] in (1, 2)
    assert body["stops"][0]["times"] == ["04:58"]
    assert body["stops"][0]["_dt"] == body["day_type"]     # 오늘의 유형으로 조회했다


# ------------------------------------------------------------- 주차 요금

def test_fee_estimate_short_term_basic_and_increments():
    # 공식 요금: 최초 10분 무료, 기본 30분 1,200원, 이후 15분당 600원(올림), 일 최대 24,000원.
    assert app.fee_estimate("단기", 10) == 0             # 회차 유예
    assert app.fee_estimate("단기", 20) == 1200          # 최초 구간 안
    assert app.fee_estimate("단기", 30) == 1200
    assert app.fee_estimate("단기", 31) == 1800          # 1분 초과도 15분 단위 올림
    assert app.fee_estimate("단기", 60) == 2400          # 30 + 15*2
    assert app.fee_estimate("단기", 10 * 60) == 24000    # 일 최대에 걸린다


def test_fee_estimate_multi_day_short_term_caps_per_day():
    two_days_3h = (24 * 2 + 3) * 60
    # 2일치 상한 + 3시간(30분 1,200 + 15분*10*600 = 7,200)
    assert app.fee_estimate("단기", two_days_3h) == 24000 * 2 + 7200


def test_fee_estimate_long_term_is_hourly_with_daily_cap():
    # 공식: 최초 10분 무료, 시간당 1,000원(올림), 일 최대 9,000원. 예전 구현은 "하루
    # 단위 올림"이라 3시간에 9,000원을 물렸다 — 공식 페이지 확인으로 바로잡았다.
    assert app.fee_estimate("장기", 10) == 0
    assert app.fee_estimate("장기", 60) == 1000
    assert app.fee_estimate("장기", 61) == 2000            # 시간 단위 올림
    assert app.fee_estimate("장기", 3 * 60) == 3000
    assert app.fee_estimate("장기", 12 * 60) == 9000       # 일 최대
    assert app.fee_estimate("장기", 24 * 60 + 3 * 60) == 9000 + 3000


def test_fee_estimate_cargo_is_the_only_large_vehicle_rate():
    # 여객 주차장에는 대형 요금이 없다 — 대형은 화물터미널 요금(최초 45분 무료,
    # 15분당 600원, 일 최대 12,000원)이다.
    assert app.fee_estimate("화물", 45) == 0
    assert app.fee_estimate("화물", 46) == 600
    assert app.fee_estimate("화물", 45 + 60) == 2400       # 15분*4
    assert app.fee_estimate("화물", 24 * 60) == 12000


def test_fee_discount_applies_the_single_highest_rate():
    # 공식: 중복 감면 불가, 높은 1개만. 경차(50%)이면서 저공해 3종(20%)이어도 50%.
    assert app.fee_discounted(24000, [50, 20]) == 12000
    assert app.fee_discounted(24000, [20]) == 19200
    assert app.fee_discounted(24000, []) == 24000


def test_pinned_fee_texts_exist_in_a_real_response():
    # 계산기의 근거 문구가 API 응답에 실재하는지 대조한다. 공항이 요금을 바꾸면 이
    # 대조가 깨져서 알게 된다 — 텍스트를 파싱해 계산하는 대신 고정값+검증을 택한 이유.
    sample = {"response": {"body": {"items": [
        {"charid": "FB00000001", "chardesc": "최초 00:30 에 한해 1200원 적용", "datetime": "x"},
        {"charid": "FB00000001", "chardesc": "00:15 초과 시 600원 부과", "datetime": "x"},
        {"charid": "NF00000001", "chardesc": "일일 최대 24000원 적용", "datetime": "x"},
        {"charid": "NF00000002", "chardesc": "일일 최대 9000원 적용", "datetime": "x"},
        {"charid": "FB00000002", "chardesc": "01:00 초과 시 1000원 부과", "datetime": "x"},
        {"charid": "NF00000003", "chardesc": "일일 최대 12000원 적용", "datetime": "x"},
    ]}}}

    assert app.fee_rules_drift(sample) == []


def test_fee_rules_drift_reports_missing_texts():
    drift = app.fee_rules_drift({"response": {"body": {"items": [
        {"charid": "FB00000001", "chardesc": "최초 00:30 에 한해 1500원 적용", "datetime": "x"},
    ]}}})

    assert any("1200" in d for d in drift)               # 사라진 근거 문구가 보고된다


def test_health_staleness_scales_with_the_source_interval(tmp_path, monkeypatch):
    # 요금은 하루 한 번 수집한다. 전역 1시간 기준을 그대로 쓰면 이 소스는 항상
    # stale로 읽혀 경보가 의미를 잃는다 — 주기의 2배까지는 정상으로 본다.
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    con = db.connect(tmp_path / "t.db")
    now = int(time.time())
    db.insert_rows(con, [(now - 60, "A", 10, 100)])
    db.upsert_passengers(con, [("2026-08-25", 9, "T1", "출국", "1", 1)], now - 60)
    db.upsert_congestion(con, [(now - 60, "T1", "DG1_E", 5, 10, 0, "00:00~24:00")])
    db.upsert_fees(con, [("FB1", "텍스트")], now - 20 * 3600)   # 20시간 전 — 하루 주기면 정상
    db.upsert_space_stats(con, [(now - 90 * 60, "T1", "01", "01", 10, 5, (1, 1, 1, 1, 1, 0))])
    con.close()

    with TestClient(app.app) as client:
        body = client.get("/api/health").json()

    assert body["sources"]["fees"]["status"] == "ok"
    assert body["status"] == "ok"


def test_fee_status_reports_drift_from_the_database(tmp_path):
    con = db.connect(tmp_path / "t.db")
    now = int(time.time())
    # 근거 문구 중 하나가 최근 수집에서 사라진 상황
    for text in app.PINNED_FEE_TEXTS[1:]:
        db.upsert_fees(con, [("X", text)], now)
    db.upsert_fees(con, [("X", app.PINNED_FEE_TEXTS[0])], now - 10 * 86400)  # 옛날엔 있었다

    missing = app.fee_status(con)

    assert missing == [app.PINNED_FEE_TEXTS[0]]


def test_fee_estimate_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with TestClient(app.app) as client:
        body = client.get("/api/fees/estimate?minutes=180&discount=compact").json()

    assert body["short"] == (1200 + 600 * 10) // 2   # 3시간, 경차 50%
    assert body["long"] == 3000 // 2                  # 시간당 1,000원 모델
    assert body["cargo"] == app.fee_discounted(app.fee_estimate("화물", 180), [50])
    assert body["discount_rate"] == 50


# ------------------------------------------------------------- 내 항공편

FLIGHTS_SAMPLE = [
    {"flightId": "KE703", "airline": "대한항공", "airport": "나리타", "scheduleDateTime": "1430",
     "estimatedDateTime": "1445", "remark": "출발", "terminalId": "P01", "gatenumber": "12",
     "chkinrange": "A01-A18", "codeshare": "Master", "masterflightid": ""},
    {"flightId": "KE5951Y", "airline": "대한항공", "airport": "두바이", "scheduleDateTime": "0005",
     "estimatedDateTime": "2347", "remark": "출발", "terminalId": "P01", "gatenumber": "43",
     "chkinrange": "H19-H32", "codeshare": "Slave", "masterflightid": "EK323Y"},
    {"flightId": "LJ201", "airline": "진에어", "airport": "괌", "scheduleDateTime": "0900",
     "estimatedDateTime": "", "remark": "", "terminalId": "P02", "gatenumber": "",
     "chkinrange": "", "codeshare": "Master", "masterflightid": ""},
]


def test_flight_search_tolerates_zero_padding_and_suffix():
    # 사용자는 'KE0703'이라 치지만 실제 ID는 'KE703'이고, 'KE5951'의 실제 ID는
    # 'KE5951Y'다. 정규화 없이 정확 일치만 하면 둘 다 못 찾는다.
    assert [f["flightId"] for f in app.flight_search(FLIGHTS_SAMPLE, "KE0703")] == ["KE703"]
    assert [f["flightId"] for f in app.flight_search(FLIGHTS_SAMPLE, "ke5951")] == ["KE5951Y"]
    assert app.flight_search(FLIGHTS_SAMPLE, "OZ999") == []


def test_flight_search_maps_concourse_to_t1():
    # P02는 탑승동 — 체크인과 주차는 T1에서 한다. P02를 그대로 내보내면 사용자가
    # 주차할 터미널을 알 수 없다.
    got = app.flight_search(FLIGHTS_SAMPLE, "LJ201")[0]
    assert got["terminal"] == "T1"
    assert got["concourse"] is True          # 탑승동임은 따로 표시한다


def test_flight_endpoint_serves_from_the_cached_list(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    async def fake_fetch(lang="K"):
        return FLIGHTS_SAMPLE

    monkeypatch.setattr(app, "fetch_departures", fake_fetch)

    with TestClient(app.app) as client:
        body = client.get("/api/flight?q=KE703").json()

    assert body["matches"][0]["flightId"] == "KE703"
    assert body["matches"][0]["scheduleDateTime"] == "1430"


# --------------------------------------------------- 주차면 집계 (체류 분포)

def test_space_aggregate_buckets_dwell_times():
    now = int(datetime(2026, 8, 25, 12, 0).timestamp())
    mk = lambda dt, status="Y": {"carstatus": status, "carindate": dt,
                                 "parklotno": "01", "parkzoneno": "01", "terno": "T1"}
    items = [
        mk("20260825113000"),        # 30분 -> 0-3h
        mk("20260825060000"),        # 6시간 -> 3-12h
        mk("20260824100000"),        # 26시간 -> 1-3일
        mk("20260817120000"),        # 8일 -> 7일+
        mk("20220101000000"),        # 4년 전 — 이상치: 점유로는 세되 히스토그램 제외
        mk("20260825110000", "N"),   # 빈 면 — 점유 아님
    ]

    rows = app.space_aggregate(items, now)

    assert len(rows) == 1
    ts, terminal, lot, zone, total, occupied, hist = rows[0]
    assert (terminal, lot, zone) == ("T1", "01", "01")
    assert total == 6 and occupied == 5
    assert hist == (1, 1, 0, 1, 0, 1)      # (0-3h, 3-12h, 12-24h, 1-3일, 3-7일, 7일+)


def test_space_aggregate_reads_t2_terminal_field():
    # T1은 terno, T2는 terminalId — 필드명이 서로 다르다.
    now = int(datetime(2026, 8, 25, 12, 0).timestamp())
    items = [{"carstatus": "N", "carindate": "", "parklotno": "12",
              "parkzoneno": "90", "terminalId": "T2"}]

    rows = app.space_aggregate(items, now)

    assert rows[0][1] == "T2"


def test_space_stats_round_trip(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_space_stats(con, [
        (1000, "T2", "12", "90", 555, 536, (10, 20, 30, 40, 50, 386)),
    ])

    r = db.space_stats_latest(con)[0]

    assert r["occupied"] == 536
    assert r["d7p"] == 386


def test_fee_endpoint_compact_beats_a_lower_discount(tmp_path, monkeypatch):
    # 경차(50%)이면서 저공해 3종(20%)을 고르면 높은 쪽만 적용돼야 한다.
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with TestClient(app.app) as client:
        b = client.get("/api/fees/estimate?minutes=180&discount=lowemission3&vehicle=compact").json()

    assert b["discount_rate"] == 50
    assert b["short"] == (1200 + 600 * 10) // 2


def test_flight_endpoint_passes_language_upstream(tmp_path, monkeypatch):
    # 항공사·도시명은 업스트림이 K/E/C로 준다 — 번역하지 말고 그대로 받아온다.
    monkeypatch.setenv("COLLECT", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    seen = []

    async def fake_fetch(lang="K"):
        seen.append(lang)
        return [dict(FLIGHTS_SAMPLE[0], airline="KOREAN AIR", airport="Tokyo/Narita")]

    monkeypatch.setattr(app, "fetch_departures", fake_fetch)

    with TestClient(app.app) as client:
        body = client.get("/api/flight?q=KE703&lang=en").json()
        client.get("/api/flight?q=KE703&lang=zh")
        client.get("/api/flight?q=KE703&lang=ko")
        client.get("/api/flight?q=KE703&lang=??")     # 모르는 값은 한국어로

    assert body["matches"][0]["airline"] == "KOREAN AIR"
    assert seen == ["E", "C", "K", "K"]
