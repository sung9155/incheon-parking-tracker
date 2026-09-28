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
"""
from collections import defaultdict
from datetime import datetime

HOUR = 3600


def cell_of(ts: int, off_dates: set) -> tuple:
    """(휴일 여부, 시각) — 계절성의 최소 단위."""
    d = datetime.fromtimestamp(ts)
    return (d.weekday() >= 5 or d.date().isoformat() in off_dates, d.hour)


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


def _anomaly(ts, nets, mean_net, off_dates):
    """그 시간이 속한 날의 순유출 이상치(명/시간). 그날 예고가 없으면 None."""
    day = _day_start(ts)
    typ = cell_of(day, off_dates)[0]
    if day not in nets or typ not in mean_net:
        return None
    return (nets[day] - mean_net[typ]) / 24


class Model:
    """한 (터미널, 종류) 그룹의 증감 모델."""

    def __init__(self, mean_delta, mean_net, k):
        self.mean_delta = mean_delta   # cell -> 평균 증감(대/시간)
        self.mean_net = mean_net       # 휴일 여부 -> 평균 일 순유출(명)
        self.k = k                     # 대/(명/시간)

    def delta(self, ts, nets, off_dates):
        """예측 증감. nets는 day_nets()의 결과 — 미래는 예고 테이블에서 온다."""
        cell = cell_of(ts, off_dates)
        if cell not in self.mean_delta:
            return None
        x = _anomaly(ts, nets, self.mean_net, off_dates)
        return self.mean_delta[cell] + (self.k * x if x is not None else 0.0)


def _cell_means(values, off_dates):
    sums = defaultdict(lambda: [0.0, 0])
    for ts, v in values.items():
        s = sums[cell_of(ts, off_dates)]
        s[0] += v
        s[1] += 1
    return {c: s / n for c, (s, n) in sums.items()}


def fit(parked, pdep, parr, off_dates) -> Model | None:
    """parked: {ts(시간 정각): 주차대수}. 증감을 만들고 계절성과 순유출 계수를 학습한다."""
    deltas = {}
    for ts in parked:
        if ts - HOUR in parked:
            deltas[ts] = parked[ts] - parked[ts - HOUR]
    if len(deltas) < 48:                      # 이틀 미만이면 계절성이 안 잡힌다
        return None

    mean_delta = _cell_means(deltas, off_dates)
    nets = day_nets(pdep, parr)
    last = max(parked)                        # 평소 순유출은 실측이 있는 날까지로 — 내일 예고는 학습이 아니다
    mean_net = _cell_means({d: n for d, n in nets.items() if d <= last}, off_dates)
    mean_net = {typ: v for (typ, _), v in mean_net.items()}   # 0시 셀 → 휴일 여부만

    # 잔차 = 증감 − 계절성. 이를 순유출 이상치 하나로 최소제곱 회귀 (절편 없음 — 평균을
    # 이미 뺐다). numpy를 들일 크기가 아니다.
    rows = []
    for ts, d in deltas.items():
        x = _anomaly(ts, nets, mean_net, off_dates)
        if x is not None:
            rows.append((x, d - mean_delta[cell_of(ts, off_dates)]))

    k = 0.0
    if len(rows) >= 48:
        sxx = sum(x * x for x, _ in rows)
        if sxx > 1e-9:
            k = sum(x * y for x, y in rows) / sxx
            # 유의하지 않은 계수는 0 — 조용한 기간에 학습된 잡음 계수가 첫 연휴에
            # 큰 편차와 곱해져 엉뚱한 방향으로 보정하는 것이 최악의 실패다.
            sse = sum((y - k * x) ** 2 for x, y in rows)
            if abs(k) < 2 * (sse / (len(rows) - 1) / sxx) ** 0.5:
                k = 0.0

    return Model(mean_delta, mean_net, k)


def forecast(model: Model, start_ts: int, start_parked: float, capacity: float,
             hours: int, pdep, parr, off_dates) -> dict:
    """start 이후 시간별 예상 주차대수. {ts: 대수}.

    적분이라 오차가 누적된다 — 지평선은 호출자가 자른다.
    상한은 capacity의 105%: 추석에 T1 장기가 105.4%를 찍었다. 만차는 벽이 아니다.
    """
    nets = day_nets(pdep, parr)
    out = {}
    parked = start_parked
    for i in range(1, hours + 1):
        ts = start_ts + i * HOUR
        d = model.delta(ts, nets, off_dates)
        if d is None:
            break
        parked = min(max(parked + d, 0.0), capacity * 1.05)
        out[ts] = parked
    return out
