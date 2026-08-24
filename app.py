import logging
from datetime import datetime

log = logging.getLogger("parking")

API_URL = "https://apis.data.go.kr/B551177/StatusOfParking/getTrackingParking"

# 순서 주의: 프로덕션이 실제로 보내는 포맷("%Y%m%d%H%M%S.%f", 초 단위 소수점 포함)이
# 맨 앞. 뒤이은 두 압축 포맷은 12자리("%Y%m%d%H%M")가 14자리("%Y%m%d%H%M%S")보다
# 먼저 와야 한다 — strptime은 %M/%S가 자릿수 제한 없이 그리디하게 매칭하므로, 14자리
# 입력을 "%Y%m%d%H%M%S"로 먼저 시도하면 "202608241305"의 마지막 두 자리가 초로
# 잘못 흡수되어 자정 근처가 아니어도 5분 단위 데이터가 조용히 어긋난다.
DATETM_FORMATS = (
    "%Y%m%d%H%M%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y%m%d%H%M",
    "%Y%m%d%H%M%S",
)


def parse_datetm(s: str) -> int:
    s = s.strip()
    for fmt in DATETM_FORMATS:
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"unknown datetm format: {s!r}")


def parse_rows(payload: dict) -> list[tuple[int, str, int, int]]:
    items = payload["response"]["body"]["items"]
    if isinstance(items, dict):
        items = items.get("item", [])
    return [
        (
            parse_datetm(it["datetm"]),
            it["floor"].strip(),
            int(it["parking"]),
            int(it["parkingarea"]),
        )
        for it in items
    ]


# scripts/probe.py 출력(실제 API 호출, 2026-08-24)으로 확정한 floor 원문 → (터미널, 유형).
# 유형은 단기/장기 외에 예약주차장(예약)이 있다 — 데이터셋 설명에는 없던 실제 3번째 유형.
FLOOR_GROUPS: dict[str, tuple[str, str]] = {
    "T1 단기주차장지하1층": ("T1", "단기"),
    "T1 단기주차장지하2층": ("T1", "단기"),
    "T1 단기주차장지하3층": ("T1", "단기"),
    "T1 단기주차장지상층": ("T1", "단기"),
    "T1 장기 P1 주차장": ("T1", "장기"),
    "T1 장기 P1 주차타워": ("T1", "장기"),
    "T1 장기 P2 주차장": ("T1", "장기"),
    "T1 장기 P2 주차타워": ("T1", "장기"),
    "T1 장기 P3 주차장": ("T1", "장기"),
    "T1 P5 예약주차장": ("T1", "예약"),
    "T2 단기주차장지하M층": ("T2", "단기"),
    "T2 단기주차장지상1층": ("T2", "단기"),
    "T2 단기주차장지상2층": ("T2", "단기"),
    "T2 단기주차장지상3층": ("T2", "단기"),
    "T2 단기주차장지상4층": ("T2", "단기"),
    "T2 장기 주차장": ("T2", "장기"),
    "T2 P1 장기주차타워": ("T2", "장기"),
    "T2 P2 장기주차타워": ("T2", "장기"),
    "T2 예약 주차장": ("T2", "예약"),
}

_warned_floors: set[str] = set()


def group_of(floor: str) -> tuple[str, str]:
    group = FLOOR_GROUPS.get(floor)
    if group is None:
        if floor not in _warned_floors:
            _warned_floors.add(floor)
            log.warning("unmapped floor %r — grouped as 기타", floor)
        return ("기타", "기타")
    return group
