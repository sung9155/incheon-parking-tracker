"""실제 API를 1회 호출해 floor 원문 문자열을 출력한다. FLOOR_GROUPS 작성용."""
import json
import os
import sys
from urllib.parse import unquote

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

# .env에는 Encoding 키 또는 Decoding 키가 들어올 수 있다. httpx params=는 값을 다시
# URL 인코딩하므로, Encoding 키를 그대로 넘기면 이중 인코딩되어 403이 난다.
# unquote는 Decoding 키(base64, %가 없음)에는 no-op이고, Encoding 키는 Decoding 키로
# 되돌려 httpx가 올바르게 인코딩하게 한다.
key = unquote(os.environ["SERVICE_KEY"])
r = httpx.get(
    app.API_URL,
    params={"serviceKey": key, "numOfRows": 100, "pageNo": 1, "type": "json"},
    timeout=15,
)
r.raise_for_status()
payload = r.json()
print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
print("--- floors ---")
for ts, floor, parked, capacity in app.parse_rows(payload):
    print(f"{floor!r}  parked={parked} capacity={capacity} ts={ts}")
