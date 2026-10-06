# 연휴 위치 템플릿 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 예측 모델이 끝난 황금연휴의 전날·첫날·중간·마지막·다음날 증감 모양을 템플릿으로 써서 다음 연휴를 하루 앞서 맞히게 한다.

**Architecture:** `forecast.py`의 모든 함수가 받던 공휴일 집합(`off_dates`)을 `Calendar` 객체(공휴일 + 황금연휴 위치)로 바꾼다. `fit`은 기본 셀(휴일 여부×시각) 평균 옆에 템플릿 셀(연휴 위치×시각) 평균을 하나 더 만들되 끝난 연휴의 증감만 넣고, `Model`은 템플릿 셀 → 기본 셀 순으로 조회한다. `app.py`는 `golden_holidays()`로 달력을 만들어 넘기고 예약 그룹에는 연휴 없는 달력을 준다.

**Tech Stack:** Python 3.13, FastAPI, SQLite, pytest (`test_app.py` 단일 파일), 외부 수치 라이브러리 없음.

**Spec:** `docs/superpowers/specs/2026-10-06-holiday-template-design.md`

## Global Constraints

- 지평선 24h(`FORECAST_MAX_HOURS = 24`), 상한 105%, 유의성 가드(|t|≥2), 순유출 변수 `k` — 바꾸지 않는다.
- 템플릿은 **끝난 연휴**(다음날까지 지나고 하루가 더 시작된 것)만. 진행 중인 연휴는 기여하지 않는다.
- 템플릿이 없으면 기본 셀로 폴백 — 첫 연휴 결과는 현행과 소수점까지 같아야 한다.
- 예약(`kind == "예약"`) 그룹은 템플릿을 쓰지 않는다.
- 순유출의 평소 기준(`mean_net`)은 평일·휴일 두 유형만.
- numpy 등 새 의존성 금지. 커밋 메시지는 한국어 요약 + `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- `datetime.fromtimestamp`는 로컬 시간(운영 컨테이너 TZ=Asia/Seoul)이다 — 테스트는 날짜를 `datetime(2026, 8, d, h)`처럼 로컬로 만든다.

## Review Focus

1. 학습창 시작이 연휴 한가운데를 자르는 경우 — 그 연휴의 일부 시각만 템플릿에 들어가도 깨지지 않고, 없는 시각은 기본 셀로 떨어져야 한다 (Task 2의 폴백 테스트가 셀 단위 폴백을 덮는다).
2. 하루짜리 연휴(start == end) — 위치는 첫날로만 매겨지고 마지막 셀은 비어 폴백한다. 예외가 나면 안 된다 (Task 1 테스트에 단일 날 케이스 포함).
3. 예고가 닿는 미래 날짜가 다음 연휴의 전날인 경우(10/8처럼) — 템플릿의 전날 셀을 써야 한다. 달력 범위가 예고 끝(+2일)까지 가는지 (Task 3 `_forecast_inputs`의 `last_day`).
4. 예약 그룹 — 템플릿을 받으면 안 된다 (Task 3 `_cal_for` 테스트).
5. 기점 당일이 연휴 다음날인 경우 — `run_end`가 다음날 다음 자정이라 아직 "끝난" 것이 아니어서 그 연휴는 템플릿이 아니다. 의도된 보수적 판정이며 Task 1의 `run_end` 테스트가 값을 고정한다.

---

### Task 1: `Calendar`와 서명 이행 (동작 불변)

**Files:**
- Modify: `forecast.py` (전체 — `cell_of`, `_anomaly`, `Model.delta`, `fit`, `forecast`의 `off_dates` → `cal`)
- Modify: `test_app.py:1491-1573` (예측 테스트들의 `set()` → `fc.Calendar()`), 새 테스트 추가
- Modify: `app.py` (`hindcast_day`, `_live_forecast`, `_hindcast`의 `off` 인자를 그대로 넘기되 타입만 Calendar — Task 3에서 실제 달력을 만들기 전까지는 `_forecast_inputs`가 `forecast.Calendar(off)`를 돌려준다)

**Interfaces:**
- Produces: `forecast.Calendar(off=(), runs=())` — `off: set[str]`, `pos: dict[str, int]`, `run_end: dict[str, int]`, 상수 `PRE=2, FIRST=3, MID=4, LAST=5, POST=6`. `cell_of(ts, cal)`, `fit(parked, pdep, parr, cal)`, `forecast(model, start_ts, start_parked, capacity, hours, pdep, parr, cal)`.

- [ ] **Step 1: 달력 테스트를 쓴다** — `test_app.py`의 `test_incomplete_day_gets_no_pax_correction` 바로 뒤에 추가

```python
def test_calendar_marks_holiday_run_positions():
    cal = fc.Calendar(off={"2026-10-05"}, runs=[(date(2026, 10, 3), date(2026, 10, 5))])
    got = [cal.pos[d] for d in ("2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06")]
    assert got == [fc.Calendar.PRE, fc.Calendar.FIRST, fc.Calendar.MID, fc.Calendar.LAST, fc.Calendar.POST]
    assert "2026-10-07" not in cal.pos
    # 끝난 연휴 판정선: 다음날(10/6)이 지나고 하루가 더 시작되는 자정
    assert cal.run_end["2026-10-04"] == int(datetime(2026, 10, 7).timestamp())
    assert "2026-10-05" in cal.off
    one_day = fc.Calendar(runs=[(date(2026, 10, 9), date(2026, 10, 9))])
    assert one_day.pos["2026-10-09"] == fc.Calendar.FIRST and one_day.pos["2026-10-10"] == fc.Calendar.POST
    assert fc.Calendar().pos == {} and fc.Calendar().off == set()
```

- [ ] **Step 2: 기존 예측 테스트의 서명을 바꾼다** — `test_app.py`에서 다음 치환을 전부 적용

| 기존 | 변경 |
|---|---|
| `fc.fit(hist, {}, {}, set())` | `fc.fit(hist, {}, {}, fc.Calendar())` |
| `fc.fit(_hourly(None, 1, profile), {}, {}, set())` | `fc.fit(_hourly(None, 1, profile), {}, {}, fc.Calendar())` |
| `fc.fit(hist, pdep, parr, set())` | `fc.fit(hist, pdep, parr, fc.Calendar())` |
| `fc.forecast(model, start, hist[start], 10_000, 6, {}, {}, set())` | 마지막 인자 `set()` → `fc.Calendar()` |
| `fc.forecast(model, start, hist[start], 120, 8, {}, {}, set())` | 마지막 인자 `set()` → `fc.Calendar()` |
| `test_incomplete_day_gets_no_pax_correction`의 두 `fc.forecast(..., set())` | 마지막 인자 → `fc.Calendar()` |
| `app.hindcast_day(hist, origin, 10_000, {}, {}, set())` | 마지막 인자 → `fc.Calendar()` |

- [ ] **Step 3: 실패를 확인한다**

Run: `python -m pytest -q test_app.py -k "calendar or forecast or fit or pax or hindcast"`
Expected: 고친 테스트 전부 FAIL (`AttributeError: module 'forecast' has no attribute 'Calendar'`). `fc.Calendar()`가 아직 없기 때문이며, 이게 Step 4가 필요한 이유다.

- [ ] **Step 4: `forecast.py`에 `Calendar`를 넣고 `off_dates`를 `cal`로 바꾼다** — 파일 전체를 아래로 교체

```python
# -*- coding: utf-8 -*-
"""여객 예고 기반 주차 점유 예측.

모델: 시간당 주차대수 증감을 두 부분으로 나눈다.
  증감(t) ≈ 평소증감[요일유형×시간] + k·(그날 순유출 − 평소 순유출)/24
  순유출 = 그날 출국예고 합 − 입국예고 합

앞부분이 "평소 이맘때 차가 이만큼 들고난다"는 계절성이고, 뒷부분이 오늘이 평소와
다른 날일 때의 보정이다. 주차대수는 흐름이 아니라 재고다 — 차는 주인이 출국할 때
들어오고 그 주인이 입국할 때 나간다. 그래서 여객 총량이 아니라 출국과 입국의 차이가
재고를 움직인다. 추석 직전 2026-09-22~24 사흘간 여객 총량은 평시와 비슷했지만 순유출이
8.2만 명이었고, 그 사흘에 전체 주차대수가 7,560대 늘었다(평시 같은 요일 구간 +3,600대).

순유출은 하루 단위로 합쳐야 보인다. 시간별 (출국−입국) 편차는 마중 차량이 들락거리는
잡음에 묻혀 계수가 죽거나 부호가 뒤집힌다 (2026-09-28 백테스트). 출국 예고와 유입의
시차(T1 3h, T2 2h)도 확인됐지만 같은 이유로 예측에는 쓰지 않는다.

현재 점유에서 출발해 증감을 앞으로 적분하므로, 지금 평소보다 가득 차 있으면 예측도
그만큼 높은 곳에서 시작한다.

요일유형은 요일 7개가 아니라 평일/휴일 2개다 — 데이터가 3주일 때 요일×시간
168칸은 칸마다 표본이 2~3개뿐이라 잡음을 학습한다. 휴일 = 주말 + 공휴일.

황금연휴는 그 위에 템플릿을 하나 더 가진다 (Calendar 참고).
"""
from collections import defaultdict
from datetime import datetime, date, time as clock, timedelta

HOUR = 3600


class Calendar:
    """예측이 보는 달력 — 공휴일과 황금연휴의 위치.

    off: 공휴일 ISO 날짜 집합. runs: 황금연휴 [(시작 date, 끝 date)] (주말·공휴일이 이어진 구간).
    연휴마다 전날·첫날·중간·마지막·다음날을 매긴다. 전날·다음날은 연휴에 붙은 날이라 항상 평일이다.
    인자 없이 만들면 연휴 없는 달력이다 — 테스트와 예약 그룹이 쓴다.
    """
    PRE, FIRST, MID, LAST, POST = 2, 3, 4, 5, 6

    def __init__(self, off=(), runs=()):
        self.off = set(off)
        self.pos: dict[str, int] = {}        # ISO 날짜 -> 위치
        self.run_end: dict[str, int] = {}    # ISO 날짜 -> 그 연휴 다음날의 다음 자정(epoch). "끝난 연휴" 판정선
        for start, end in runs:
            days = [start + timedelta(days=i) for i in range(-1, (end - start).days + 2)]
            finished_at = int(datetime.combine(end + timedelta(days=2), clock.min).timestamp())
            for d in days:
                self.pos[d.isoformat()] = (self.PRE if d < start else self.POST if d > end
                                           else self.FIRST if d == start else self.LAST if d == end else self.MID)
                self.run_end[d.isoformat()] = finished_at


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def cell_of(ts: int, cal: Calendar) -> tuple:
    """(휴일 여부, 시각) — 계절성의 최소 단위. 연휴 날도 여기서는 그냥 휴일이다."""
    d = datetime.fromtimestamp(ts)
    return (d.weekday() >= 5 or d.date().isoformat() in cal.off, d.hour)


def _day_start(ts: int) -> int:
    return int(datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def day_nets(pdep, parr) -> dict:
    """{그날 0시 ts: 출국 합 − 입국 합}. 24시간이 다 있는 날만 — 반나절 합은 순유출이 아니다."""
    out = {}
    for day in {_day_start(ts) for ts in pdep}:
        hours = [day + h * HOUR for h in range(24)]
        if all(h in pdep and h in parr for h in hours):
            out[day] = sum(pdep[h] - parr[h] for h in hours)
    return out


def _anomaly(ts, nets, mean_net, cal):
    """그 시간이 속한 날의 순유출 이상치(명/시간). 그날 예고가 없으면 None."""
    day = _day_start(ts)
    typ = cell_of(day, cal)[0]
    if day not in nets or typ not in mean_net:
        return None
    return (nets[day] - mean_net[typ]) / 24


class Model:
    """한 (터미널, 종류) 그룹의 증감 모델."""

    def __init__(self, mean_delta, mean_net, k):
        self.mean_delta = mean_delta   # cell -> 평균 증감(대/시간)
        self.mean_net = mean_net       # 휴일 여부 -> 평균 일 순유출(명)
        self.k = k                     # 대/(명/시간)

    def seasonal(self, ts, cal):
        """평소 증감. 셀이 없으면 None."""
        return self.mean_delta.get(cell_of(ts, cal))

    def delta(self, ts, nets, cal):
        """예측 증감. nets는 day_nets()의 결과 — 미래는 예고 테이블에서 온다."""
        base = self.seasonal(ts, cal)
        if base is None:
            return None
        x = _anomaly(ts, nets, self.mean_net, cal)
        return base + (self.k * x if x is not None else 0.0)


def _cell_means(values, cal, key=cell_of):
    sums = defaultdict(lambda: [0.0, 0])
    for ts, v in values.items():
        s = sums[key(ts, cal)]
        s[0] += v
        s[1] += 1
    return {c: s / n for c, (s, n) in sums.items()}


def fit(parked, pdep, parr, cal: Calendar) -> Model | None:
    """parked: {ts(시간 정각): 주차대수}. 증감을 만들고 계절성과 순유출 계수를 학습한다."""
    deltas = {}
    for ts in parked:
        if ts - HOUR in parked:
            deltas[ts] = parked[ts] - parked[ts - HOUR]
    if len(deltas) < 48:                      # 이틀 미만이면 계절성이 안 잡힌다
        return None

    mean_delta = _cell_means(deltas, cal)
    nets = day_nets(pdep, parr)
    last = max(parked)                        # 평소 순유출은 실측이 있는 날까지로 — 내일 예고는 학습이 아니다
    mean_net = _cell_means({d: n for d, n in nets.items() if d <= last}, cal)
    mean_net = {typ: v for (typ, _), v in mean_net.items()}   # 0시 셀 → 휴일 여부만
    model = Model(mean_delta, mean_net, 0.0)

    # 잔차 = 증감 − 계절성. 이를 순유출 이상치 하나로 최소제곱 회귀 (절편 없음 — 평균을
    # 이미 뺐다). numpy를 들일 크기가 아니다.
    rows = []
    for ts, d in deltas.items():
        x = _anomaly(ts, nets, mean_net, cal)
        base = model.seasonal(ts, cal)
        if x is not None and base is not None:
            rows.append((x, d - base))

    if len(rows) >= 48:
        sxx = sum(x * x for x, _ in rows)
        if sxx > 1e-9:
            k = sum(x * y for x, y in rows) / sxx
            # 유의하지 않은 계수는 0 — 조용한 기간에 학습된 잡음 계수가 첫 연휴에
            # 큰 편차와 곱해져 엉뚱한 방향으로 보정하는 것이 최악의 실패다.
            sse = sum((y - k * x) ** 2 for x, y in rows)
            if abs(k) >= 2 * (sse / (len(rows) - 1) / sxx) ** 0.5:
                model.k = k

    return model


def forecast(model: Model, start_ts: int, start_parked: float, capacity: float,
             hours: int, pdep, parr, cal: Calendar) -> dict:
    """start 이후 시간별 예상 주차대수. {ts: 대수}.

    적분이라 오차가 누적된다 — 지평선은 호출자가 자른다.
    상한은 capacity의 105%: 추석에 T1 장기가 105.4%를 찍었다. 만차는 벽이 아니다.
    """
    nets = day_nets(pdep, parr)
    out = {}
    parked = start_parked
    for i in range(1, hours + 1):
        ts = start_ts + i * HOUR
        d = model.delta(ts, nets, cal)
        if d is None:
            break
        parked = min(max(parked + d, 0.0), capacity * 1.05)
        out[ts] = parked
    return out
```

이 단계에서 `Model.seasonal`은 기본 셀만 본다(템플릿은 Task 2). `fit`의 잔차 계산을 `model.seasonal`로 바꾼 것은 Task 2에서 템플릿 셀을 쓰게 하기 위한 준비이며 지금은 동작이 같다.

- [ ] **Step 5: `app.py`의 호출부 타입을 맞춘다** — `_off_dates`의 반환을 Calendar로 감싼다

`_forecast_inputs`의 마지막 줄을 바꾼다:

```python
    return parked, caps, pdep, parr, forecast.Calendar(_off_dates(first_day, last_day))
```

`hindcast_day`, `_live_forecast`, `_hindcast`는 이미 `off`라는 이름으로 그 값을 `forecast.fit`/`forecast.forecast`에 넘기므로 코드 변경이 없다. 변수 이름은 Task 3에서 `cal`로 바꾼다.

- [ ] **Step 6: 전체 테스트가 통과하는지 확인한다**

Run: `python -m pytest -q test_app.py`
Expected: 전부 PASS (기존 114 + 1).

- [ ] **Step 7: 커밋**

```bash
git add forecast.py app.py test_app.py
git commit -m "refactor: 예측 달력을 Calendar로 — 공휴일 집합에 황금연휴 위치를 더할 자리

동작은 그대로다. forecast.py의 모든 함수가 off_dates 대신 Calendar를 받고,
Calendar는 황금연휴마다 전날·첫날·중간·마지막·다음날과 끝난 시점을 매긴다.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 템플릿 셀 — 끝난 연휴의 증감을 다음 연휴에 쓴다

**Files:**
- Modify: `forecast.py` (`Model.__init__`/`seasonal`, `fit`, 새 `template_cell`)
- Modify: `test_app.py` (Task 1의 달력 테스트 뒤에 테스트 2개 추가)

**Interfaces:**
- Consumes: Task 1의 `Calendar`, `cell_of`, `_cell_means(values, cal, key)`.
- Produces: `template_cell(ts, cal) -> tuple | None`, `Model(mean_delta, tmpl_delta, mean_net, k)`, `Model.seasonal(ts, cal)`가 템플릿 셀 → 기본 셀 순으로 조회.

- [ ] **Step 1: 실패하는 테스트 둘을 쓴다** — `test_calendar_marks_holiday_run_positions` 뒤에 추가

```python
def _world_with_holiday_runs():
    """8/8(토)~8/10(월, 공휴일) 연휴와 8/15(토)~8/17(월, 공휴일) 연휴.
    증감은 매시 +5, 첫 연휴 첫날(8/8)만 매시 +20. 데이터는 8/14 23시까지."""
    cal = fc.Calendar(off={"2026-08-10", "2026-08-17"},
                      runs=[(date(2026, 8, 8), date(2026, 8, 10)), (date(2026, 8, 15), date(2026, 8, 17))])
    hist, parked = {}, 1000.0
    for d in range(14):                                   # 8/1 ~ 8/14
        for h in range(24):
            parked += 20 if d == 7 else 5
            hist[int(datetime(2026, 8, 1 + d, h).timestamp())] = parked
    return cal, hist


def test_finished_holiday_run_becomes_the_template_for_the_next():
    # 8/14 23시 기점의 다음 6시간은 8/15 0~5시 = 두 번째 연휴의 첫날. 끝난 첫 연휴의
    # 첫날(8/8) 증감 +20이 템플릿이라 +20씩 올라가야 한다. 휴일 셀 평균(+5와 +20의 섞임)이면 실패.
    cal, hist = _world_with_holiday_runs()
    model = fc.fit(hist, {}, {}, cal)
    start = max(hist)
    pred = fc.forecast(model, start, hist[start], 1e9, 6, {}, {}, cal)
    steps = [pred[start + i * 3600] - (pred[start + (i - 1) * 3600] if i > 1 else hist[start]) for i in range(1, 7)]
    assert [round(s) for s in steps] == [20] * 6


def test_in_progress_first_run_falls_back_to_plain_cells():
    # 끝난 연휴가 없는 첫 연휴 한가운데(8/15 12시) 기점 — 템플릿이 없으니 연휴 없는
    # 달력으로 적합한 것과 예측이 완전히 같아야 한다 (현행 동작 보존).
    cal = fc.Calendar(off={"2026-08-17"}, runs=[(date(2026, 8, 15), date(2026, 8, 17))])
    plain = fc.Calendar(off={"2026-08-17"})
    hist, parked = {}, 1000.0
    for d in range(15):
        for h in range(24 if d < 14 else 13):             # 8/15는 12시까지
            parked += 20 if d == 14 else 5
            hist[int(datetime(2026, 8, 1 + d, h).timestamp())] = parked
    start = max(hist)
    with_runs = fc.forecast(fc.fit(hist, {}, {}, cal), start, hist[start], 1e9, 24, {}, {}, cal)
    without = fc.forecast(fc.fit(hist, {}, {}, plain), start, hist[start], 1e9, 24, {}, {}, plain)
    assert with_runs == without
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest -q test_app.py -k "template_for_the_next or falls_back_to_plain"`
Expected: `test_finished_holiday_run_becomes_the_template_for_the_next` FAIL (steps가 20이 아님 — 휴일 셀 평균). `test_in_progress_first_run_falls_back_to_plain_cells`는 PASS (아직 템플릿이 없으므로 당연히 같다) — 이 테스트는 Task 2 구현 뒤에도 계속 통과해야 하는 회귀 가드다.

- [ ] **Step 3: 템플릿 셀을 구현한다** — `forecast.py`

`cell_of` 바로 뒤에 추가:

```python
def template_cell(ts: int, cal: Calendar) -> tuple | None:
    """(연휴 위치, 시각). 연휴에 속한 날(전날·다음날 포함)이 아니면 None."""
    d = datetime.fromtimestamp(ts)
    pos = cal.pos.get(d.date().isoformat())
    return None if pos is None else (pos, d.hour)
```

`Model`을 교체:

```python
class Model:
    """한 (터미널, 종류) 그룹의 증감 모델."""

    def __init__(self, mean_delta, tmpl_delta, mean_net, k):
        self.mean_delta = mean_delta   # (휴일 여부, 시각) -> 평균 증감(대/시간)
        self.tmpl_delta = tmpl_delta   # (연휴 위치, 시각) -> 평균 증감 — 끝난 연휴에서만 배운 템플릿
        self.mean_net = mean_net       # 휴일 여부 -> 평균 일 순유출(명)
        self.k = k                     # 대/(명/시간)

    def seasonal(self, ts, cal):
        """평소 증감: 템플릿 셀이 있으면 그것, 없으면 기본 셀, 그것도 없으면 None.

        첫 연휴에는 템플릿이 없어 현행과 똑같이 움직이고, 두 번째 연휴부터 앞 연휴의
        모양(전날 쌓이고, 첫날 치솟고, 마지막 날 빠지는)을 쓴다.
        """
        t = template_cell(ts, cal)
        if t is not None and t in self.tmpl_delta:
            return self.tmpl_delta[t]
        return self.mean_delta.get(cell_of(ts, cal))

    def delta(self, ts, nets, cal):
        """예측 증감. nets는 day_nets()의 결과 — 미래는 예고 테이블에서 온다."""
        base = self.seasonal(ts, cal)
        if base is None:
            return None
        x = _anomaly(ts, nets, self.mean_net, cal)
        return base + (self.k * x if x is not None else 0.0)
```

`fit`에서 `mean_delta = _cell_means(deltas, cal)` 바로 뒤에 추가하고 `Model(...)` 생성을 바꾼다:

```python
    # 템플릿: 끝난 연휴의 증감만. 진행 중인 연휴를 넣으면 첫날 급증이 뒷날에 그대로
    # 복사돼 마지막 날을 과대 예측한다 (2026-10-06 백테스트, 추석 +7.6%p).
    today = _day_start(max(parked))
    finished = {ts: d for ts, d in deltas.items() if cal.run_end.get(_iso(ts), today + 1) <= today}
    tmpl_delta = _cell_means(finished, cal, template_cell)
```

```python
    model = Model(mean_delta, tmpl_delta, mean_net, 0.0)
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `python -m pytest -q test_app.py`
Expected: 전부 PASS (117개). 특히 `test_in_progress_first_run_falls_back_to_plain_cells`가 여전히 PASS.

- [ ] **Step 5: 커밋**

```bash
git add forecast.py test_app.py
git commit -m "feat: 끝난 황금연휴의 증감 모양을 다음 연휴의 템플릿으로

연휴 위치(전날·첫날·중간·마지막·다음날)×시각 셀을 기본 셀 위에 하나 더 두고,
끝난 연휴의 증감만 넣는다. 템플릿이 없으면 기본 셀로 떨어져 첫 연휴는 현행과
같다. 개천절 백테스트(추석이 템플릿): 19-24h MAE T1 단기 12.6→5.5, T2 단기
8.5→3.8, T1 장기 8.0→4.6, T2 장기 5.4→2.7.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `app.py` 달력 연결, 예약 제외, README

**Files:**
- Modify: `app.py` (`_off_dates` → `_calendar`, `_forecast_inputs`, `_live_forecast`, `hindcast_day`, `_hindcast`, 새 `_cal_for`)
- Modify: `test_app.py` (`_cal_for` 테스트 1개)
- Modify: `README.md` 예측 절

**Interfaces:**
- Consumes: `forecast.Calendar(off, runs)`, `golden_holidays(from_value, to_value) -> [{"start", "end", ...}]`, `parse_date`.
- Produces: `app._calendar(first: date, last: date) -> forecast.Calendar`, `app._cal_for(kind: str, cal) -> forecast.Calendar`.

- [ ] **Step 1: 예약 제외 테스트를 쓴다** — `test_in_progress_first_run_falls_back_to_plain_cells` 뒤에 추가

```python
def test_reserved_lots_get_a_calendar_without_holiday_runs():
    cal = fc.Calendar(off={"2026-10-05"}, runs=[(date(2026, 10, 3), date(2026, 10, 5))])
    assert app._cal_for("단기", cal) is cal
    plain = app._cal_for("예약", cal)
    assert plain.pos == {} and plain.off == cal.off
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest -q test_app.py -k reserved_lots`
Expected: FAIL (`AttributeError: module 'app' has no attribute '_cal_for'`).

- [ ] **Step 3: `app.py`를 고친다**

`_off_dates` 함수를 통째로 아래로 교체:

```python
def _calendar(first: date, last: date) -> forecast.Calendar:
    """구간의 공휴일과 황금연휴. 주말은 forecast.cell_of가 스스로 안다.

    연휴는 golden_holidays()로 직접 가져온다 — /api/holidays는 데이터 범위로 잘라내므로
    예고가 닿는 미래의 연휴(전날 예측에 필요)가 빠진다.
    """
    calendar = holidays.country_holidays(
        "KR", years=list(range(first.year, last.year + 1)), language="ko"
    )
    off = set()
    day = first
    while day <= last:
        name = calendar.get(day)
        if name is not None and _base_name(name) not in NOT_A_DAY_OFF:
            off.add(day.isoformat())
        day += timedelta(days=1)
    runs = [(parse_date(r["start"]), parse_date(r["end"]))
            for r in golden_holidays(first.isoformat(), last.isoformat())]
    return forecast.Calendar(off, runs)


def _cal_for(kind: str, cal: forecast.Calendar) -> forecast.Calendar:
    """예약 주차장은 연휴 템플릿을 쓰면 나빠진다(2026-10-06 백테스트) — 연휴 없는 달력을 준다."""
    return cal if kind != "예약" else forecast.Calendar(cal.off)
```

`_forecast_inputs`의 마지막 줄:

```python
    return parked, caps, pdep, parr, _calendar(first_day, last_day)
```

`hindcast_day`의 서명과 본문에서 `off` → `cal` (의미만 바뀌고 로직은 같다):

```python
def hindcast_day(hist, origin: int, cap: float, pdep, parr, cal) -> dict:
    """origin(어느 날 0시)에 알던 것만으로 낸 24시간 예측 — 지나간 날의 예측선을 재현한다.

    학습 창은 현재 예측과 같은 60일이되 origin에서 끝난다. 그날 실측을 훔쳐보면
    재현이 아니라 사후 설명이 된다. 연휴 템플릿도 origin 전에 끝난 연휴만 쓴다.
    """
    train = {t: v for t, v in hist.items() if origin - FORECAST_TRAIN_DAYS * 86400 <= t <= origin}
    if origin not in train:
        return {}
    model = forecast.fit(train, pdep, parr, cal)
    if model is None:
        return {}
    return forecast.forecast(model, origin, train[origin], cap, FORECAST_MAX_HOURS, pdep, parr, cal)
```

`_live_forecast`의 루프 안 두 호출:

```python
    parked, caps, pdep, parr, cal = _forecast_inputs(con, now - FORECAST_TRAIN_DAYS * 86400, now)
    out = []
    for (term, kind), hist in parked.items():
        kcal = _cal_for(kind, cal)
        model = forecast.fit(hist, pdep.get(term, {}), parr.get(term, {}), kcal)
        if model is None:
            continue
        last_ts = max(hist)
        cap = caps[(term, kind)][last_ts]
        future_pax = [ts for ts in pdep.get(term, {}) if ts > last_ts]
        if not future_pax or not cap:
            continue
        hours = min((max(future_pax) - last_ts) // forecast.HOUR, FORECAST_MAX_HOURS)
        pred = forecast.forecast(model, last_ts, hist[last_ts], cap, hours,
                                 pdep[term], parr.get(term, {}), kcal)
        out.extend(_forecast_rows(term, kind, cap, pred))
```

`_hindcast`에서 `parked, caps, pdep, parr, off = ...` → `cal`로, 그리고 `hindcast_day(...)` 호출의 마지막 인자를 `_cal_for(kind, cal)`로:

```python
    parked, caps, pdep, parr, cal = _forecast_inputs(con, since, now)
```

```python
                pred = hindcast_day(hist, origin, cap, pdep.get(term, {}), parr.get(term, {}), _cal_for(kind, cal)) if cap else {}
```

- [ ] **Step 4: 전체 테스트**

Run: `python -m pytest -q test_app.py`
Expected: 전부 PASS (118개). `grep -n "_off_dates\|off_dates" app.py forecast.py test_app.py`가 아무것도 찾지 않아야 한다.

- [ ] **Step 5: README 예측 절에 한 항목 추가** — "지나간 시각에도 예측선이 있다" 항목 바로 뒤

```markdown
- **연휴 위치 템플릿**: 황금연휴 날들은 전날·첫날·중간·마지막·다음날 위치별 셀을 따로 가진다.
  **끝난 연휴만 템플릿**이 되고 템플릿이 없으면 평일·휴일 셀로 떨어진다 — 그래서 첫 연휴(추석)는
  현행과 같고, 두 번째부터 앞 연휴의 모양(전날 쌓이고, 첫날 치솟고, 마지막 날 빠지는)을 쓴다.
  개천절 검증(2026-10-06, 추석 하나가 템플릿): 19~24h MAE T1 단기 12.6→5.5, T2 단기 8.5→3.8,
  T1 장기 8.0→4.6, T2 장기 5.4→2.7, 12시간 만차 경보 놓침 4→1·오경보 2→0. 체류 분포 변수와
  히스토그램 보정도 같이 시험했지만 첫날을 앞서 보지 못하거나 표본 밖에서 불안정해 접었다.
  예약 주차장은 템플릿을 쓰면 나빠져 제외한다
```

- [ ] **Step 6: 커밋**

```bash
git add app.py test_app.py README.md
git commit -m "feat: 예측에 황금연휴 달력을 연결 — 예약 주차장은 템플릿 제외

_calendar()가 공휴일과 golden_holidays()의 연휴 구간으로 Calendar를 만들어
현재 예측과 자정 기점 재현에 넘긴다. /api/holidays는 데이터 범위로 잘라내므로
쓰지 않는다 — 예고가 닿는 다음 연휴의 전날 예측에 미래 연휴가 필요하다.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 운영 데이터 재현 검증과 배포

**Files:**
- Create (scratchpad, 저장소 밖): `holiday_template_verify.py` — 저장소 `forecast.py`로 운영 데이터 워크포워드
- 저장소 변경 없음 (검증 수치가 스펙의 표와 맞는지 확인만)

**Interfaces:**
- Consumes: `forecast.fit/forecast(…, cal)`, `app.golden_holidays`, 운영 API `/api/series`, `/api/passengers`, `/api/dayoffs`.

- [ ] **Step 1: 검증 스크립트를 쓴다** — 세션 스크랩패드 디렉터리에 저장

```python
# -*- coding: utf-8 -*-
"""저장소 forecast.py가 탐색(holiday_pos2.py '연휴위치 v3')의 수치를 재현하는지 — 기점 9/13~10/6 매일 4회, 24h."""
import json, sys, urllib.request
from collections import defaultdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

REPO = r"C:/Users/ksg91/orca/workspaces/airportParkingInfo/palolo"
sys.path.insert(0, REPO)
import forecast
KST = ZoneInfo("Asia/Seoul")
class _DT(datetime):
    @classmethod
    def fromtimestamp(cls, ts, tz=None): return datetime.fromtimestamp(ts, KST)
forecast.datetime = _DT          # 서버는 TZ=Asia/Seoul — 셀 계산을 그와 맞춘다
HOUR, DAY = 3600, 86400
BASE = "https://airportparking.sgkwak.duckdns.org"
def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r: return json.load(r)
def ts_of(d, h=0): return int(datetime(d.year, d.month, d.day, h, tzinfo=KST).timestamp())

series = get("/api/series?from=2026-07-20&to=2026-10-06&bucket=3600")
pax = get("/api/passengers?from=2026-07-20&to=2026-10-08")
off = {d["date"] for d in get("/api/dayoffs?from=2026-07-01&to=2026-10-31")}
runs = [(date.fromisoformat(r["start"]), date.fromisoformat(r["end"])) for r in get("/api/holidays?from=2026-08-01&to=2026-10-31")]
cal, plain = forecast.Calendar(off, runs), forecast.Calendar(off)
parked, caps = defaultdict(dict), defaultdict(dict)
for r in series:
    g = (r["terminal"], r["kind"])
    parked[g][r["ts"]] = parked[g].get(r["ts"], 0.0) + (r["capacity"] - r["available"])
    caps[g][r["ts"]] = caps[g].get(r["ts"], 0.0) + r["capacity"]
pdep, parr = defaultdict(dict), defaultdict(dict)
for r in pax:
    ts = ts_of(datetime.fromisoformat(r["adate"]).date(), r["hour"])
    side = pdep if r["direction"] == "출국" else parr
    side[r["terminal"]][ts] = side[r["terminal"]].get(ts, 0.0) + r["expected"]

def period(o):
    if ts_of(date(2026, 9, 23)) <= o < ts_of(date(2026, 9, 29)): return "추석"
    if ts_of(date(2026, 10, 2)) <= o < ts_of(date(2026, 10, 7)): return "개천절"
    return "평시"
errs = defaultdict(list)
for day in [date(2026, 9, 13) + timedelta(days=i) for i in range(24)]:
    for hh in (0, 6, 12, 18):
        origin = ts_of(day, hh)
        for g, hist in parked.items():
            if origin not in hist or g[1] == "예약": continue
            train = {t: v for t, v in hist.items() if origin - 60 * DAY <= t <= origin}
            known = ts_of(day + timedelta(days=2))
            pd = {t: v for t, v in pdep[g[0]].items() if t < known}
            pa = {t: v for t, v in parr[g[0]].items() if t < known}
            cap = caps[g][origin]; fut = [t for t in pd if t > origin]
            hours = min((max(fut) - origin) // HOUR, 24) if fut else 0
            for name, c in (("현행(연휴 없음)", plain), ("템플릿", cal)):
                m = forecast.fit(train, pd, pa, c)
                if m is None: continue
                for t, p in forecast.forecast(m, origin, hist[origin], cap, hours, pd, pa, c).items():
                    if t in hist and 19 <= (t - origin) // HOUR <= 24:
                        errs[(name, g, period(origin))].append(abs(p - hist[t]) / cap * 100)
mae = lambda xs: sum(xs) / len(xs) if xs else float("nan")
print("19-24h MAE(%p)  현행 → 템플릿   (기대: 평시·추석 동일, 개천절 T1단기 5.5 / T2단기 3.8 / T1장기 4.6 / T2장기 2.7)")
for g in sorted(parked):
    if g[1] == "예약": continue
    print(f"  {g[0]} {g[1]}: " + " | ".join(f"{per} {mae(errs[('현행(연휴 없음)', g, per)]):4.1f} → {mae(errs[('템플릿', g, per)]):4.1f}" for per in ("평시", "추석", "개천절")))
```

- [ ] **Step 2: 실행해 스펙의 표와 맞는지 본다**

Run: `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python holiday_template_verify.py`
Expected: 추석·평시 열은 현행과 같고(±0.1), 개천절 열은 스펙 표(5.5 / 3.8 / 4.6 / 2.7)와 ±0.2 안. 벗어나면 Task 2의 `finished` 판정이나 `seasonal` 조회 순서를 의심한다.

- [ ] **Step 3: master 푸시와 재배포** — 사용자 확인 후, 기존 절차대로

```bash
git push origin HEAD:master
git -C "D:/Utility/airportParkingInfo" merge --ff-only origin/master
```

Portainer(`https://portainer.sgkwak.duckdns.org`, 스택 airport-parking-tracker) → Pull and redeploy → Update. 운영 확인: `/api/health` 정상, 컨테이너 재생성 시각, `/api/forecast?from=2026-10-01&to=<오늘>`의 10/3~10/5 재현 행이 바뀐 수치(템플릿 적용)인지.

- [ ] **Step 4: 메모리와 다음 검증 약속** — 한글날 연휴(10/9~10/11) 뒤 10/12 이후 검증: 자정 기점 재현 일별 MAE, 12h 만차 경보, 개천절 수치와 비교.
