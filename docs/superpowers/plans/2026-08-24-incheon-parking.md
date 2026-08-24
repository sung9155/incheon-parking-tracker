# 인천공항 주차 가용면수 수집·시각화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 인천공항 주차 가용면수를 5분마다 수집·적재하고, 실측 시계열과 요일×시간 평균 패턴을 웹에서 보여준다.

**Architecture:** 단일 FastAPI 프로세스가 `lifespan`에서 asyncio 수집 루프를 돌리면서 동시에 `/api/*`와 정적 파일을 서빙한다. 저장은 SQLite 단일 테이블, 파생값은 조회 시 계산한다. 미니 PC의 도커에서 컨테이너 1개로 구동한다.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, httpx, 표준 라이브러리 sqlite3, Chart.js(CDN), Docker Compose

**Spec:** `docs/superpowers/specs/2026-08-24-incheon-parking-design.md`

## Global Constraints

- Python 3.13 이상. 베이스 이미지 `python:3.13-slim`.
- 런타임 의존성은 `fastapi`, `uvicorn`, `httpx` 세 개뿐. ORM·마이그레이션 툴·스케줄러 라이브러리를 추가하지 않는다.
- 테스트 의존성은 `pytest` 하나. conftest나 픽스처 파일을 만들지 않는다.
- 모든 테스트는 `test_app.py` 단일 파일에 누적한다.
- 수집 주기는 300초(5분) 고정. 개발계정 일 1,000 트래픽 대비 288회.
- `ts` 컬럼은 항상 epoch seconds 정수. 시간대 변환은 조회 시점에만 `'localtime'`으로 수행한다.
- 컨테이너 환경변수 `TZ=Asia/Seoul` 필수. 미설정 시 요일×시간 패턴이 9시간 밀린다.
- 브라우저 Geolocation API를 절대 사용하지 않는다 (위치기반서비스 신고 회피, 스펙 2절).
- 가용 면수(`capacity - parked`)를 DB에 저장하지 않는다. 조회 시 계산한다.
- API 호출 실패 시 재시도하지 않는다. 로그만 남기고 다음 틱으로 넘어간다.

### 스펙 대비 파일 구성 변경

스펙 10절은 `app.py` 단일 모듈을 제시했으나, SQL 로직(특히 버킷팅·패턴 쿼리)을 HTTP 계층 없이 테스트하기 위해 **`db.py`와 `app.py` 두 모듈로 분리**한다. 그 외 파일 구성은 스펙과 같다.

- `db.py` — 스키마, 삽입, 모든 조회 쿼리. FastAPI를 import하지 않는다.
- `app.py` — API 호출·파싱, 그룹 매핑, 수집 루프, 라우트, 정적 파일 마운트.

또한 스펙 8절은 테스트 2개를 최소선으로 제시했으나, 이 계획은 TDD로 진행하므로 태스크별로
누적해 총 18개가 된다. 파일은 스펙대로 `test_app.py` 하나를 유지한다.

---

### Task 1: 프로젝트 뼈대와 DB 계층

멱등 삽입이 이 시스템의 중복 방지 전략 전체다. PK `(ts, floor)` + `INSERT OR IGNORE`로, API의 `datetm`이 아직 갱신되지 않았을 때 같은 행이 조용히 무시되게 한다. 별도 중복 체크 코드를 두지 않는다.

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `db.py`
- Test: `test_app.py`

**Interfaces:**
- Consumes: (없음)
- Produces:
  - `db.connect(path=None) -> sqlite3.Connection` — 디렉터리 생성, WAL 설정, 스키마 생성까지 수행. `path`가 None이면 환경변수 `DB_PATH`, 그것도 없으면 `data/parking.db`.
  - `db.insert_rows(con: sqlite3.Connection, rows: Iterable[tuple[int, str, int, int]]) -> None` — `(ts, floor, parked, capacity)` 튜플을 멱등 삽입.

- [ ] **Step 1: 의존성 파일과 .gitignore 작성**

`requirements.txt`:

```
fastapi
uvicorn
httpx
pytest
```

`.gitignore`:

```
__pycache__/
*.pyc
.env
data/
.pytest_cache/
```

- [ ] **Step 2: 의존성 설치**

Run: `python -m pip install -r requirements.txt`
Expected: fastapi, uvicorn, httpx, pytest 설치 완료

- [ ] **Step 3: 실패하는 테스트 작성**

`test_app.py`:

```python
import db


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
```

- [ ] **Step 4: 테스트 실패 확인**

Run: `python -m pytest test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 5: db.py 구현**

```python
import os
import sqlite3
from pathlib import Path
from typing import Iterable

DEFAULT_DB_PATH = "data/parking.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS parking (
  ts       INTEGER NOT NULL,
  floor    TEXT    NOT NULL,
  parked   INTEGER NOT NULL,
  capacity INTEGER NOT NULL,
  PRIMARY KEY (ts, floor)
) WITHOUT ROWID;
"""


def connect(path=None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("DB_PATH") or DEFAULT_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def insert_rows(con: sqlite3.Connection, rows: Iterable[tuple[int, str, int, int]]) -> None:
    con.executemany(
        "INSERT OR IGNORE INTO parking (ts, floor, parked, capacity) VALUES (?, ?, ?, ?)",
        rows,
    )
    con.commit()
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (2 passed)

- [ ] **Step 7: 커밋**

```bash
git add requirements.txt .gitignore db.py test_app.py
git commit -m "feat: add sqlite schema with idempotent insert"
```

---

### Task 2: 공공 API 응답 파싱

응답 스키마에 두 가지 미확정 지점이 있어 방어적으로 파싱한다. 첫째, `items`가 리스트로 오는지 `{"item": [...]}` 로 감싸여 오는지 data.go.kr 서비스마다 다르다. 둘째, `datetm`의 문자열 포맷이 문서화되어 있지 않다. 두 경우 모두 후보를 시도하되, 전부 실패하면 조용히 넘어가지 말고 예외를 던진다.

**Files:**
- Create: `app.py`
- Modify: `test_app.py`

**Interfaces:**
- Consumes: (없음)
- Produces:
  - `app.API_URL: str`
  - `app.parse_datetm(s: str) -> int` — `datetm` 문자열을 epoch seconds로 변환. 알려진 포맷이 없으면 `ValueError`.
  - `app.parse_rows(payload: dict) -> list[tuple[int, str, int, int]]` — API JSON 응답을 `db.insert_rows`가 받는 튜플 리스트로 변환.

- [ ] **Step 1: 실패하는 테스트 작성**

`test_app.py` 하단에 추가:

```python
from datetime import datetime

import pytest

import app


def test_parse_datetm_accepts_known_formats():
    expected = int(datetime(2026, 8, 24, 13, 5).timestamp())
    assert app.parse_datetm("2026-08-24 13:05") == expected
    assert app.parse_datetm("202608241305") == expected
    assert app.parse_datetm("2026-08-24 13:05:00") == expected
    assert app.parse_datetm("20260824130500") == expected


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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: app.py 구현**

```python
import os
from datetime import datetime

API_URL = "https://apis.data.go.kr/B551177/StatusOfParking/getTrackingParking"

DATETM_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 커밋**

```bash
git add app.py test_app.py
git commit -m "feat: parse public data API response into db rows"
```

---

### Task 3: 실제 API 응답 확인과 구역 그룹 매핑

**이 태스크는 사용자의 `SERVICE_KEY`가 필요하다.** 스펙 4절이 미확정으로 남긴 `floor` 원문 문자열과 터미널 소속을 여기서 확정한다.

주의: data.go.kr은 인증키를 Encoding/Decoding 두 가지로 제공한다. httpx가 파라미터를 다시 URL 인코딩하므로 반드시 **Decoding 키**를 써야 한다. Encoding 키를 넣으면 `%2B`가 `%252B`로 이중 인코딩되어 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`가 난다.

매핑에 없는 `floor` 값은 버리지 않고 `("기타", "기타")` 그룹으로 흘려보내면서 경고 로그를 남긴다. 매핑이 불완전해도 데이터는 온전히 쌓인다.

**Files:**
- Create: `.env.example`
- Create: `scripts/probe.py`
- Modify: `app.py`
- Modify: `test_app.py`

**Interfaces:**
- Consumes: `app.API_URL`, `app.parse_rows` (Task 2)
- Produces:
  - `app.FLOOR_GROUPS: dict[str, tuple[str, str]]` — `floor` 원문 → `(터미널, 유형)`.
  - `app.group_of(floor: str) -> tuple[str, str]` — 매핑에 없으면 `("기타", "기타")`.

- [ ] **Step 1: `.env.example` 작성**

```
# data.go.kr 인증키 — 반드시 "일반 인증키(Decoding)" 값을 넣을 것
SERVICE_KEY=여기에_디코딩_키
DB_PATH=data/parking.db
TZ=Asia/Seoul
```

- [ ] **Step 2: 탐침 스크립트 작성**

`scripts/probe.py`:

```python
"""실제 API를 1회 호출해 floor 원문 문자열을 출력한다. FLOOR_GROUPS 작성용."""
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

key = os.environ["SERVICE_KEY"]
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
```

- [ ] **Step 3: 실제 호출로 floor 문자열 확인**

Run: `SERVICE_KEY=<디코딩키> python scripts/probe.py`
Expected: 9개 내외의 `floor` 원문이 출력된다.

`datetm` 파싱이 `ValueError`로 실패하면, 출력된 원문 포맷을 `app.DATETM_FORMATS`에 추가하고 Task 2의 `test_parse_datetm_accepts_known_formats`에도 해당 케이스를 추가한 뒤 다시 실행한다.

- [ ] **Step 4: 실패하는 테스트 작성**

`test_app.py` 하단에 추가:

```python
def test_every_known_floor_is_mapped():
    assert app.FLOOR_GROUPS, "FLOOR_GROUPS is empty — run scripts/probe.py first"
    for floor, group in app.FLOOR_GROUPS.items():
        assert app.group_of(floor) == group


def test_unknown_floor_falls_back_to_etc():
    assert app.group_of("존재하지 않는 주차장") == ("기타", "기타")
```

- [ ] **Step 5: 테스트 실패 확인**

Run: `python -m pytest test_app.py -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute 'FLOOR_GROUPS'`

- [ ] **Step 6: 매핑 구현**

Step 3의 출력에서 얻은 원문 문자열로 아래 dict의 키를 **실제 값으로 교체**한다. 값은 `(터미널, 유형)` 이며 터미널은 `"T1"`/`"T2"`, 유형은 `"단기"`/`"장기"` 를 쓴다. 주석 처리된 항목은 데이터셋 설명에 근거한 추정이므로 실제 출력과 다르면 실제 출력을 따른다.

`app.py`에 추가:

```python
import logging

log = logging.getLogger("parking")

# scripts/probe.py 출력으로 확정한 실제 floor 원문을 키로 쓴다.
FLOOR_GROUPS: dict[str, tuple[str, str]] = {
    # "단기주차장 지하1층": ("T1", "단기"),
    # "단기주차장 지하2층": ("T1", "단기"),
    # "단기주차장 지상층":  ("T1", "단기"),
    # "장기주차장 P1":     ("T1", "장기"),
    # "장기주차장 P2":     ("T1", "장기"),
    # "장기주차장 P3":     ("T1", "장기"),
    # "장기주차장 P4":     ("T1", "장기"),
    # "주차타워 P1":       ("T2", "단기"),
    # "주차타워 P2":       ("T2", "단기"),
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
```

- [ ] **Step 7: 테스트 통과 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (8 passed). `FLOOR_GROUPS`가 비어 있으면 첫 테스트가 실패하므로, Step 6에서 주석을 실제 값으로 반드시 교체해야 한다.

- [ ] **Step 8: 커밋**

```bash
git add .env.example scripts/probe.py app.py test_app.py
git commit -m "feat: map floor strings to terminal and parking type groups"
```

---

### Task 4: 현재 현황 조회

**Files:**
- Modify: `db.py`
- Modify: `test_app.py`

**Interfaces:**
- Consumes: `db.connect`, `db.insert_rows` (Task 1)
- Produces:
  - `db.latest(con: sqlite3.Connection) -> list[sqlite3.Row]` — 가장 최근 `ts`의 전 구역 행. 컬럼: `ts`, `floor`, `parked`, `capacity`, `available`. 데이터가 없으면 빈 리스트.

- [ ] **Step 1: 실패하는 테스트 작성**

`test_app.py` 하단에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest test_app.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'latest'`

- [ ] **Step 3: 구현**

`db.py`에 추가:

```python
def latest(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT ts, floor, parked, capacity, capacity - parked AS available
        FROM parking
        WHERE ts = (SELECT MAX(ts) FROM parking)
        """
    ).fetchall()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: 커밋**

```bash
git add db.py test_app.py
git commit -m "feat: query latest parking snapshot"
```

---

### Task 5: 시계열 조회와 다운샘플링

조회 기간이 3일을 초과하면 1시간 버킷 평균으로 접는다. 30일 조회 시 구역당 8,640포인트라 그대로 보내면 브라우저가 버벅인다.

버킷의 `ts`는 `(ts / 3600) * 3600` — SQLite의 정수 나눗셈이 버림이므로 각 버킷의 시작 시각이 된다.

**Files:**
- Modify: `db.py`
- Modify: `test_app.py`

**Interfaces:**
- Consumes: `db.connect`, `db.insert_rows` (Task 1)
- Produces:
  - `db.BUCKET_THRESHOLD_SECONDS: int` = `3 * 86400`
  - `db.BUCKET_SECONDS: int` = `3600`
  - `db.series(con, start: int, end: int) -> list[sqlite3.Row]` — `start <= ts <= end` 구간. 컬럼: `ts`, `floor`, `available`. 구간 길이가 `BUCKET_THRESHOLD_SECONDS`를 초과하면 1시간 버킷 평균(`available`은 float), 아니면 원시 해상도(정수).

- [ ] **Step 1: 실패하는 테스트 작성**

`test_app.py` 하단에 추가:

```python
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

    assert [r["ts"] for r in rows] == [500]


def test_series_separates_floors(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.insert_rows(con, [(0, "A", 10, 100), (0, "B", 20, 100)])

    rows = db.series(con, 0, DAY)

    assert sorted((r["floor"], r["available"]) for r in rows) == [("A", 90), ("B", 80)]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest test_app.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'series'`

- [ ] **Step 3: 구현**

`db.py`에 추가:

```python
BUCKET_THRESHOLD_SECONDS = 3 * 86400
BUCKET_SECONDS = 3600

_SERIES_RAW = """
SELECT ts, floor, capacity - parked AS available
FROM parking
WHERE ts BETWEEN ? AND ?
ORDER BY ts, floor
"""

_SERIES_BUCKETED = """
SELECT (ts / ?) * ? AS ts, floor, AVG(capacity - parked) AS available
FROM parking
WHERE ts BETWEEN ? AND ?
GROUP BY 1, floor
ORDER BY 1, floor
"""


def series(con: sqlite3.Connection, start: int, end: int) -> list[sqlite3.Row]:
    if end - start > BUCKET_THRESHOLD_SECONDS:
        return con.execute(
            _SERIES_BUCKETED, (BUCKET_SECONDS, BUCKET_SECONDS, start, end)
        ).fetchall()
    return con.execute(_SERIES_RAW, (start, end)).fetchall()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (14 passed)

- [ ] **Step 5: 커밋**

```bash
git add db.py test_app.py
git commit -m "feat: query time series with hourly downsampling past 3 days"
```

---

### Task 6: 요일×시간 평균 패턴 조회

`ts`는 UTC 기준 epoch이므로 `'localtime'` 변환 없이 묶으면 요일과 시간이 밀린다. 테스트는 어느 시간대에서 실행되든 통과해야 하므로, 기대값을 상수로 박지 말고 `datetime.fromtimestamp`(로컬 시간대 기준)로 계산해서 비교한다.

표본 수를 함께 반환한다. 데이터가 2~3주 쌓이기 전에는 칸이 비어 있는데, 화면이 그 사실을 표시해야 하기 때문이다.

**Files:**
- Modify: `db.py`
- Modify: `test_app.py`

**Interfaces:**
- Consumes: `db.connect`, `db.insert_rows` (Task 1)
- Produces:
  - `db.pattern(con: sqlite3.Connection) -> list[sqlite3.Row]` — 컬럼: `dow`(0=일요일 ~ 6=토요일, 로컬 시간대 기준), `hour`(0~23), `floor`, `available`(평균, float), `samples`(int).

- [ ] **Step 1: 실패하는 테스트 작성**

`test_app.py` 하단에 추가:

```python
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


def test_pattern_separates_distinct_hours(tmp_path):
    con = db.connect(tmp_path / "t.db")
    base = int(datetime(2026, 8, 24, 15, 0).timestamp())
    db.insert_rows(con, [(base, "A", 40, 100), (base + 2 * HOUR, "A", 10, 100)])

    rows = db.pattern(con)

    assert len(rows) == 2
    assert sorted(r["hour"] for r in rows) == sorted(
        [datetime.fromtimestamp(base).hour, datetime.fromtimestamp(base + 2 * HOUR).hour]
    )
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest test_app.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'pattern'`

- [ ] **Step 3: 구현**

`db.py`에 추가:

```python
_PATTERN = """
SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS dow,
       CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,
       floor,
       AVG(capacity - parked) AS available,
       COUNT(*) AS samples
FROM parking
GROUP BY dow, hour, floor
ORDER BY dow, hour, floor
"""


def pattern(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(_PATTERN).fetchall()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (16 passed)

- [ ] **Step 5: 커밋**

```bash
git add db.py test_app.py
git commit -m "feat: query weekday-by-hour availability pattern in local time"
```

---

### Task 7: 수집 루프와 HTTP 라우트

`COLLECT=0`으로 수집 루프를 끌 수 있게 한다. 개발 중 `uvicorn --reload`가 루프를 중복 기동하는 것과, 테스트 실행 시 외부 호출이 나가는 것을 한 줄로 함께 막는다.

응답 JSON은 구역별 행에 `terminal`/`kind`를 붙여서 내보낸다. 그룹 합산은 프런트에서 한다 — 어차피 화면이 그룹 단위와 구역 단위를 오가므로 서버가 두 형태를 다 만들 이유가 없다.

**Files:**
- Create: `static/index.html` (빈 파일, Task 8에서 채움)
- Modify: `app.py`
- Modify: `test_app.py`

**Interfaces:**
- Consumes: `app.API_URL`, `app.parse_rows` (Task 2), `app.group_of` (Task 3), `db.connect`/`db.insert_rows` (Task 1), `db.latest` (Task 4), `db.series` (Task 5), `db.pattern` (Task 6)
- Produces:
  - `app.app: FastAPI` — ASGI 앱
  - `app.COLLECT_INTERVAL_SECONDS: int` = 300
  - `app.collect_once(client: httpx.AsyncClient, con, key: str) -> int` — 1회 수집. 삽입 시도한 행 수를 반환. 예외를 잡지 않는다.
  - `GET /api/current` → `db.latest` 결과에 `terminal`/`kind` 추가
  - `GET /api/series?days=N` (기본 1) → 지금부터 N일 전까지
  - `GET /api/pattern`
  - 정적 파일이 `/`에 마운트됨

- [ ] **Step 1: 실패하는 테스트 작성**

`test_app.py` 하단에 추가:

```python
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

    assert [r["ts"] for r in rows] == [now - 600]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest test_app.py -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute 'app'`

- [ ] **Step 3: static 디렉터리 준비**

`app.mount`가 존재하는 디렉터리를 요구한다.

Run: `mkdir -p static && touch static/index.html`

- [ ] **Step 4: 구현**

`app.py` 상단 import에 추가:

```python
import asyncio
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db
```

`app.py` 하단에 추가:

```python
COLLECT_INTERVAL_SECONDS = 300


async def collect_once(client: httpx.AsyncClient, con, key: str) -> int:
    r = await client.get(
        API_URL,
        params={"serviceKey": key, "numOfRows": 100, "pageNo": 1, "type": "json"},
        timeout=15,
    )
    r.raise_for_status()
    rows = parse_rows(r.json())
    for _, floor, _, _ in rows:
        group_of(floor)          # 미매핑 구역 경고를 남긴다
    db.insert_rows(con, rows)
    return len(rows)


async def collect_loop(con) -> None:
    key = os.environ["SERVICE_KEY"]
    async with httpx.AsyncClient() as client:
        while True:
            try:
                n = await collect_once(client, con, key)
                log.info("collected %d rows", n)
            except Exception:
                # 재시도하지 않는다. 5분 뒤 다음 틱이 온다.
                log.exception("collect failed")
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    con = db.connect()
    _app.state.con = con
    task = None
    if os.environ.get("COLLECT", "1") == "1":
        task = asyncio.create_task(collect_loop(con))
    yield
    if task is not None:
        task.cancel()
    con.close()


app = FastAPI(lifespan=lifespan)


def _with_group(row) -> dict:
    d = dict(row)
    d["terminal"], d["kind"] = group_of(d["floor"])
    return d


@app.get("/api/current")
def api_current():
    return [_with_group(r) for r in db.latest(app.state.con)]


@app.get("/api/series")
def api_series(days: int = 1):
    end = int(time.time())
    return [_with_group(r) for r in db.series(app.state.con, end - days * 86400, end)]


@app.get("/api/pattern")
def api_pattern():
    return [_with_group(r) for r in db.pattern(app.state.con)]


app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

`logging.basicConfig`는 uvicorn이 이미 핸들러를 설정하므로 추가하지 않는다.

- [ ] **Step 5: 테스트 통과 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (18 passed)

- [ ] **Step 6: 실제 수집 1회 확인**

Run: `SERVICE_KEY=<디코딩키> python -m uvicorn app:app --port 8000`
다른 터미널에서: `curl http://localhost:8000/api/current`
Expected: 로그에 `collected N rows`, `/api/current`가 구역별 JSON 배열을 반환.

`unmapped floor` 경고가 뜨면 Task 3의 `FLOOR_GROUPS`를 보완하고 별도 커밋한다.

- [ ] **Step 7: 커밋**

```bash
git add app.py test_app.py static/index.html
git commit -m "feat: add collector loop and query endpoints"
```

---

### Task 8: 프런트엔드 화면

빌드 스텝 없이 정적 HTML 하나. Chart.js는 CDN에서 로드한다.

아래 코드는 구조와 데이터 흐름을 확정한 것이며, 차트의 시각적 세부(색상 팔레트, 축 눈금, 범례, 툴팁)는 Step 1에서 읽은 `dataviz` 스킬의 지침에 맞춰 조정한다.

Geolocation API를 쓰지 않는다.

**Files:**
- Modify: `static/index.html`

**Interfaces:**
- Consumes: `GET /api/current`, `GET /api/series?days=N`, `GET /api/pattern` (Task 7)
- Produces: (없음 — 최종 소비자)

- [ ] **Step 1: dataviz 스킬 확인**

`dataviz` 스킬을 읽고 차트 색상·축·범례 지침을 파악한다.

- [ ] **Step 2: 화면 구현**

`static/index.html`:

```html
<!doctype html>
<meta charset="utf-8">
<title>인천공항 주차 현황</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; padding: 0 1rem; }
  .cards { display: flex; flex-wrap: wrap; gap: 1rem; }
  .card { border: 1px solid #ccc; border-radius: .5rem; padding: 1rem; min-width: 9rem; }
  .card b { display: block; font-size: 2rem; }
  .tabs { margin: 1.5rem 0 .5rem; }
  .tabs button[aria-selected="true"] { font-weight: bold; }
  .hidden { display: none; }
  #stamp, #thin { color: #666; font-size: .9rem; }
</style>

<h1>인천공항 주차 현황</h1>
<p id="stamp"></p>
<div class="cards" id="cards"></div>

<div class="tabs">
  <button id="tab-series" aria-selected="true">실측 추이</button>
  <button id="tab-pattern" aria-selected="false">요일×시간 패턴</button>
</div>

<div id="panel-series">
  <label>기간
    <select id="days">
      <option value="1">1일</option>
      <option value="7" selected>7일</option>
      <option value="30">30일</option>
    </select>
  </label>
  <canvas id="chart-series" height="120"></canvas>
</div>

<div id="panel-pattern" class="hidden">
  <label>그룹 <select id="group"></select></label>
  <p id="thin" class="hidden">표본이 부족한 시간대가 있습니다. 데이터가 더 쌓이면 정확해집니다.</p>
  <canvas id="chart-pattern" height="120"></canvas>
</div>

<script>
const groupKey = r => `${r.terminal} ${r.kind}`;

// 구역별 행을 그룹 단위로 합산한다. keyFields는 그룹 외 추가 축(ts 또는 dow+hour).
function sumByGroup(rows, keyFields) {
  const acc = new Map();
  for (const r of rows) {
    const k = JSON.stringify([groupKey(r), ...keyFields.map(f => r[f])]);
    acc.set(k, (acc.get(k) ?? 0) + r.available);
  }
  return acc;
}

async function loadCurrent() {
  const rows = await (await fetch('/api/current')).json();
  const totals = sumByGroup(rows, []);
  document.getElementById('cards').innerHTML = [...totals]
    .map(([k, v]) => `<div class="card">${JSON.parse(k)[0]}<b>${Math.round(v)}</b>자리</div>`)
    .join('');
  if (rows.length) {
    document.getElementById('stamp').textContent =
      '갱신: ' + new Date(rows[0].ts * 1000).toLocaleString('ko-KR');
  }
  const sel = document.getElementById('group');
  const groups = [...new Set(rows.map(groupKey))];
  if (sel.options.length !== groups.length) {
    sel.innerHTML = groups.map(g => `<option>${g}</option>`).join('');
  }
}

let seriesChart, patternChart;

async function loadSeries() {
  const days = document.getElementById('days').value;
  const rows = await (await fetch(`/api/series?days=${days}`)).json();
  const groups = [...new Set(rows.map(groupKey))];
  const stamps = [...new Set(rows.map(r => r.ts))].sort((a, b) => a - b);
  const totals = sumByGroup(rows, ['ts']);

  const datasets = groups.map(g => ({
    label: g,
    data: stamps.map(ts => totals.get(JSON.stringify([g, ts])) ?? null),
    spanGaps: true,
    pointRadius: 0,
  }));

  seriesChart?.destroy();
  seriesChart = new Chart(document.getElementById('chart-series'), {
    type: 'line',
    data: {
      labels: stamps.map(ts => new Date(ts * 1000).toLocaleString('ko-KR')),
      datasets,
    },
    options: { scales: { y: { title: { display: true, text: '가용 면수' } } } },
  });
}

const DOW = ['일', '월', '화', '수', '목', '금', '토'];

async function loadPattern() {
  const rows = await (await fetch('/api/pattern')).json();
  const target = document.getElementById('group').value;
  const mine = rows.filter(r => groupKey(r) === target);

  const avail = sumByGroup(mine, ['dow', 'hour']);

  // 그룹 합계의 표본 수는 구성 구역 중 최소값으로 본다.
  const samples = new Map();
  for (const r of mine) {
    const k = JSON.stringify([groupKey(r), r.dow, r.hour]);
    samples.set(k, Math.min(samples.get(k) ?? Infinity, r.samples));
  }
  document.getElementById('thin').classList.toggle(
    'hidden', ![...samples.values()].some(n => n < 3));

  const hours = [...Array(24).keys()];
  const datasets = DOW.map((name, dow) => ({
    label: name,
    data: hours.map(h => avail.get(JSON.stringify([target, dow, h])) ?? null),
    spanGaps: true,
  }));

  patternChart?.destroy();
  patternChart = new Chart(document.getElementById('chart-pattern'), {
    type: 'line',
    data: { labels: hours.map(h => `${h}시`), datasets },
    options: { scales: { y: { title: { display: true, text: '평균 가용 면수' } } } },
  });
}

function showTab(which) {
  for (const t of ['series', 'pattern']) {
    document.getElementById(`tab-${t}`).setAttribute('aria-selected', String(t === which));
    document.getElementById(`panel-${t}`).classList.toggle('hidden', t !== which);
  }
  (which === 'series' ? loadSeries : loadPattern)();
}

document.getElementById('tab-series').onclick = () => showTab('series');
document.getElementById('tab-pattern').onclick = () => showTab('pattern');
document.getElementById('days').onchange = loadSeries;
document.getElementById('group').onchange = loadPattern;

loadCurrent().then(loadSeries);
setInterval(loadCurrent, 60_000);
</script>
```

- [ ] **Step 3: 브라우저에서 확인**

Run: `SERVICE_KEY=<디코딩키> python -m uvicorn app:app --port 8000`
`http://localhost:8000/` 접속.
Expected: 현황 카드가 그룹 수만큼 뜨고, 실측 추이 탭에 선이 그려진다. 수집 직후라 포인트가 1개뿐일 수 있다 — 몇 틱 기다린 뒤 다시 확인한다. 요일×시간 패턴 탭에는 표본 부족 안내가 뜨는 것이 정상이다.

- [ ] **Step 4: 테스트가 여전히 통과하는지 확인**

Run: `python -m pytest test_app.py -v`
Expected: PASS (18 passed)

- [ ] **Step 5: 커밋**

```bash
git add static/index.html
git commit -m "feat: add dashboard with time series and weekly pattern charts"
```

---

### Task 9: 도커 패키징과 문서

미니 PC의 도커에서 구동한다. 개발 머신에 도커가 없으므로 빌드 검증은 미니 PC에서 수행한다.

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `compose.yml`
- Create: `README.md`

**Interfaces:**
- Consumes: `app.app` (Task 7)
- Produces: (없음 — 최종 산출물)

- [ ] **Step 1: Dockerfile 작성**

```dockerfile
FROM python:3.13-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py app.py ./
COPY static/ ./static/

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: .dockerignore 작성**

```
data/
.env
.git/
__pycache__/
.pytest_cache/
docs/
scripts/
test_app.py
```

- [ ] **Step 3: compose.yml 작성**

```yaml
services:
  parking:
    build: .
    restart: unless-stopped
    ports: ["8000:8000"]
    volumes: ["./data:/data"]
    environment:
      TZ: Asia/Seoul
      SERVICE_KEY: ${SERVICE_KEY}
      DB_PATH: /data/parking.db
```

- [ ] **Step 4: README 작성**

`README.md`:

````markdown
# 인천공항 주차 현황

인천국제공항의 주차 가용 면수를 5분마다 수집해 적재하고, 실측 추이와 요일×시간 평균
패턴을 보여준다. 공공 API가 당일 데이터만 주기 때문에 과거 이력은 직접 쌓는다.

## 준비

1. [공공데이터포털](https://www.data.go.kr/data/15095047/openapi.do)에서
   "인천국제공항공사_주차 정보" 활용신청 (자동승인).
2. `.env` 작성:

   ```
   SERVICE_KEY=<일반 인증키(Decoding) 값>
   ```

   **Encoding 키가 아니라 Decoding 키를 넣어야 한다.** httpx가 파라미터를 다시 URL
   인코딩하므로 Encoding 키는 이중 인코딩되어 인증에 실패한다.

## 실행

```bash
docker compose up -d --build
```

`http://<미니PC주소>:8000` 접속.

`./data`는 로컬 디스크여야 한다. NAS나 네트워크 마운트에 두면 SQLite WAL이 깨진다.

## 개발

```bash
pip install -r requirements.txt
python -m pytest test_app.py -v                      # 외부 호출 없이 실행됨
SERVICE_KEY=<키> python -m uvicorn app:app --reload   # 로컬 구동
```

`COLLECT=0`이면 수집 루프가 뜨지 않는다.

## 문서

- 설계: `docs/superpowers/specs/2026-08-24-incheon-parking-design.md`
- 구현 계획: `docs/superpowers/plans/2026-08-24-incheon-parking.md`

## 법적 사항

위치기반서비스 사업자 신고 대상이 아니다. 주차 잔여 면수는 전기통신설비로 측위된
위치정보가 아니라 시설 점유 현황 통계이며, 개인도 이동성 있는 물건도 식별하지 않는다.

**브라우저 Geolocation API를 사용하지 않는다.** 사용자 위치를 받는 순간 신고 대상이
되며(저장하지 않아도 동일), 미신고 시 3년 이하 징역 또는 3천만원 이하 벌금이다.
````

- [ ] **Step 5: 미니 PC에서 빌드·구동 확인**

미니 PC에서:

```bash
docker compose up -d --build
docker compose logs -f parking
```

Expected: 즉시 `collected N rows` 로그. `curl http://localhost:8000/api/current`가 구역별 JSON 반환.

- [ ] **Step 6: 컨테이너 시간대 확인**

Run: `docker compose exec parking date`
Expected: KST 시각이 출력된다. UTC가 나오면 `TZ` 설정이 적용되지 않은 것이며, 요일×시간 패턴이 9시간 밀린다.

- [ ] **Step 7: 커밋**

```bash
git add Dockerfile .dockerignore compose.yml README.md
git commit -m "feat: package as docker compose service"
```

---

## 완료 후 남는 것

- 요일×시간 패턴 탭은 데이터가 2~3주 쌓이기 전까지 빈약하다. 표본 부족 안내가 뜨는 것이 정상이다.
- 스펙 11절의 범위 밖 항목(ORM, 마이그레이션, 인증, 보존 정책, 재시도 백오프, 알림, 헬스체크, TLS)은 의도적으로 구현하지 않는다.
