# 인천공항 주차 현황

인천국제공항의 주차 가용 면수를 5분마다 수집해 적재하고, 실측 추이와 요일×시간 평균
패턴을 보여준다. 공공 API가 당일 데이터만 주기 때문에 과거 이력은 직접 쌓는다.

실시간 현황은 T1, T2의 3가지 주차 유형(단기/장기/예약) 전체 19개 구역을 대시보드로
확인할 수 있다. 요일×시간 패턴은 2~3주 데이터가 쌓인 후부터 확인 가능하다.

## 준비

1. [공공데이터포털](https://www.data.go.kr/data/15095047/openapi.do)에서
   "인천국제공항공사_주차 정보" 활용신청 (자동승인).
2. `.env` 작성:

   ```
   SERVICE_KEY=<인증키>
   ```

   일반 인증키(Encoding 또는 Decoding 모두 가능)를 입력하면 된다.

## 실행

```bash
docker compose up -d --build
```

`http://<미니PC주소>:8000` 접속.

`./data`는 로컬 디스크여야 한다. NAS나 네트워크 마운트에 두면 SQLite WAL이 깨진다.

## 개발

```bash
pip install -r requirements-dev.txt
python -m pytest test_app.py -v                      # 외부 호출 없이 실행됨 (30개 테스트)
COLLECT=0 SERVICE_KEY=<키> python -m uvicorn app:app --reload   # 로컬 구동, 수집기는 끈다
```

`COLLECT=0`이면 수집 루프가 뜨지 않는다.

## 운영

시간대가 틀리면 요일×시간 패턴만 어긋나는 게 아니다. `parse_datetm`이 naive local time으로
`ts`를 만들기 때문에, `TZ`가 빠지거나 `tzdata` 패키지가 없으면 저장되는 **모든** 타임스탬프가
9시간 밀린다. 그러면 `db.latest`의 신선도 창(최근 1시간)에 걸리는 행이 하나도 없게 되어
`/api/current`가 빈 배열을 반환하고, **카드도 두 차트도 전부 빈 화면**이 된다. 대시보드가
통째로 비어 있다면 먼저 이 문제를 의심하라.

설정 후 다음을 실행해서 `1970-01-01 09:00:00` (KST)가 나오는지 확인하라.

```bash
docker compose exec parking python -c "import sqlite3; print(sqlite3.connect(':memory:').execute(\"SELECT datetime(0,'unixepoch','localtime')\").fetchone()[0])"
```

`00:00:00`이 나오면 `tzdata` 패키지가 없어서 UTC로 고정된 것이다.

## 문서

- 설계: `docs/superpowers/specs/2026-08-24-incheon-parking-design.md`
- 구현 계획: `docs/superpowers/plans/2026-08-24-incheon-parking.md`

## 법적 사항

위치기반서비스 사업자 신고 대상이 아니다. 주차 잔여 면수는 전기통신설비로 측위된
위치정보가 아니라 시설 점유 현황 통계이며, 개인도 이동성 있는 물건도 식별하지 않는다.

**브라우저 Geolocation API를 사용하지 않는다.** 사용자 위치를 받는 순간 신고 대상이
되며(저장하지 않아도 동일), 미신고 시 3년 이하 징역 또는 3천만원 이하 벌금이다.
