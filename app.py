from datetime import datetime

API_URL = "https://apis.data.go.kr/B551177/StatusOfParking/getTrackingParking"

DATETM_FORMATS = (
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
