# -*- coding: utf-8 -*-
"""여객 예고 기반 주차 점유 예측.

모델: 시간당 주차대수 증감을 두 부분으로 나눈다.
  증감(t) ≈ 평소증감[요일유형×시간] + a·출국예고편차(t+lag) + b·입국예고편차(t)

앞부분이 "평소 이맘때 차가 이만큼 들고난다"는 계절성이고, 뒷부분이 여객 예고가
평소와 다를 때(연휴 첫날 등)의 보정이다. 예고 편차 항 덕분에 이 모델은 요일×시간
평균만 쓰는 기존 '평소' 기준선이 못 보는 것 — 오늘이 평소와 다른 날이라는 사실 —
을 본다. 현재 점유에서 출발해 증감을 앞으로 적분하므로, 지금 평소보다 가득 차
있으면 예측도 그만큼 높은 곳에서 시작한다.

요일유형은 요일 7개가 아니라 평일/휴일 2개다 — 데이터가 3주일 때 요일×시간
168칸은 칸마다 표본이 2~3개뿐이라 잡음을 학습한다. 휴일 = 주말 + 공휴일.

상관 분석(2026-08-24~09-12, n=444시간)으로 확정한 시차: 출국 예고 대비 단기주차
유입 피크가 T1은 3시간 전, T2는 2시간 전 (r≈0.78~0.79, 1주차와 3주차 값이 동일).
"""
from collections import defaultdict
from datetime import datetime

HOUR = 3600

# 출국예고가 주차 유입에 앞서는 시간. 상관 분석의 최적 lag (모듈 docstring 참고).
LAGS = {"T1": 3, "T2": 2}


def cell_of(ts: int, off_dates: set) -> tuple:
    """(휴일 여부, 시각) — 계절성의 최소 단위."""
    d = datetime.fromtimestamp(ts)
    return (d.weekday() >= 5 or d.date().isoformat() in off_dates, d.hour)


class Model:
    """한 (터미널, 종류) 그룹의 증감 모델."""

    def __init__(self, mean_delta, mean_pdep, mean_parr, a, b, lag):
        self.mean_delta = mean_delta   # cell -> 평균 증감(대/시간)
        self.mean_pdep = mean_pdep     # cell -> 평균 출국예고(명)
        self.mean_parr = mean_parr     # cell -> 평균 입국예고(명)
        self.a, self.b, self.lag = a, b, lag

    def delta(self, ts, pdep, parr, off_dates):
        """예측 증감. pdep/parr는 {ts: 명} — 미래는 예고 테이블에서 온다."""
        cell = cell_of(ts, off_dates)
        if cell not in self.mean_delta:
            return None
        out = self.mean_delta[cell]
        dep_ts = ts + self.lag * HOUR
        dep_cell = cell_of(dep_ts, off_dates)
        if dep_ts in pdep and dep_cell in self.mean_pdep:
            out += self.a * (pdep[dep_ts] - self.mean_pdep[dep_cell])
        if ts in parr and cell in self.mean_parr:
            out += self.b * (parr[ts] - self.mean_parr[cell])
        return out


def _cell_means(values, off_dates):
    sums = defaultdict(lambda: [0.0, 0])
    for ts, v in values.items():
        s = sums[cell_of(ts, off_dates)]
        s[0] += v
        s[1] += 1
    return {c: s / n for c, (s, n) in sums.items()}


def fit(parked, pdep, parr, off_dates, lag) -> Model | None:
    """parked: {ts(시간 정각): 주차대수}. 증감을 만들고 계절성과 예고 계수를 학습한다."""
    deltas = {}
    for ts in parked:
        if ts - HOUR in parked:
            deltas[ts] = parked[ts] - parked[ts - HOUR]
    if len(deltas) < 48:                      # 이틀 미만이면 계절성이 안 잡힌다
        return None

    mean_delta = _cell_means(deltas, off_dates)
    mean_pdep = _cell_means(pdep, off_dates)
    mean_parr = _cell_means(parr, off_dates)

    # 잔차 = 증감 − 계절성. 이를 예고 편차 2개로 최소제곱 회귀 (절편 없음 — 평균을
    # 이미 뺐다). 2×2 정규방정식을 직접 푼다. numpy를 들일 크기가 아니다.
    rows = []
    for ts, d in deltas.items():
        cell = cell_of(ts, off_dates)
        dep_ts = ts + lag * HOUR
        dep_cell = cell_of(dep_ts, off_dates)
        if dep_ts not in pdep or ts not in parr:
            continue
        x1 = pdep[dep_ts] - mean_pdep.get(dep_cell, pdep[dep_ts])
        x2 = parr[ts] - mean_parr.get(cell, parr[ts])
        rows.append((x1, x2, d - mean_delta[cell]))

    a = b = 0.0
    if len(rows) >= 48:
        s11 = sum(x1 * x1 for x1, _, _ in rows)
        s22 = sum(x2 * x2 for _, x2, _ in rows)
        s12 = sum(x1 * x2 for x1, x2, _ in rows)
        s1y = sum(x1 * y for x1, _, y in rows)
        s2y = sum(x2 * y for _, x2, y in rows)
        det = s11 * s22 - s12 * s12
        if det > 1e-9:
            a = (s1y * s22 - s2y * s12) / det
            b = (s2y * s11 - s1y * s12) / det
            # 유의하지 않은 계수는 0 — 조용한 기간에 학습된 잡음 계수가 첫 연휴에
            # 큰 예고 편차와 곱해져 엉뚱한 방향으로 보정하는 것이 최악의 실패다.
            n = len(rows)
            sse = sum((y - a * x1 - b * x2) ** 2 for x1, x2, y in rows)
            sigma2 = sse / (n - 2)
            if abs(a) < 2 * (sigma2 * s22 / det) ** 0.5:
                a = 0.0
            if abs(b) < 2 * (sigma2 * s11 / det) ** 0.5:
                b = 0.0

    return Model(mean_delta, mean_pdep, mean_parr, a, b, lag)


def forecast(model: Model, start_ts: int, start_parked: float, capacity: float,
             hours: int, pdep, parr, off_dates) -> dict:
    """start 이후 시간별 예상 주차대수. {ts: 대수}.

    적분이라 오차가 누적된다 — 지평선은 호출자가 여객 예고가 닿는 데까지로 자른다.
    상한은 capacity의 105%: 실측에서 T1 장기가 100.2%를 찍었다. 만차는 벽이 아니다.
    """
    out = {}
    parked = start_parked
    for i in range(1, hours + 1):
        ts = start_ts + i * HOUR
        d = model.delta(ts, pdep, parr, off_dates)
        if d is None:
            break
        parked = min(max(parked + d, 0.0), capacity * 1.05)
        out[ts] = parked
    return out
