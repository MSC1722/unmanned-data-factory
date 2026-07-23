# 무인 데이터 공장

해외 마켓(이베이, 야후옥션 등)에서 특정 키워드의 매물 데이터를 수집하는
FastAPI 기반 초경량 비동기 스크레이핑 파이프라인. AI 분석 단계는 아직 붙지 않은
1단계(수집 → 검증 → 로깅 → 적재) 구현이다.

## 프로젝트 구조

```
app/
  main.py                 FastAPI 엔트리포인트
  core/
    config.py              환경변수 기반 전역 설정 (pydantic-settings)
    logger.py               콘솔 + 파일 로깅 설정
  schemas/
    product.py               ProductItem / ScrapeJobResult Pydantic 모델 (범용)
    camera.py                  카메라/렌즈 도메인 모델 (RawCameraItem, ProcessedCameraItem 등)
  scrapers/
    base.py                  스크레이퍼 추상 인터페이스 (fetch → parse)
    ebay_mock.py               이베이 스타일 스크레이퍼 (mock/실전 겸용, 범용 파이프라인용)
    ebay_rss.py                 이베이 공개 RSS 검색 피드 수집기 (카메라 도메인 실데이터 소스)
  services/
    pipeline.py               스크레이퍼 실행 → 로깅 → outbox 적재 오케스트레이션
    ai_gemini.py                Gemini Vision 외관 상태 분석 (실패 시 규칙 기반 fallback)
    ai_claude.py                 Claude 텍스트 기반 스캠 위험도 분석 (실패 시 규칙 기반 fallback)
    ai_cache.py                   AI 응답 SQLite 캐시 (item_id 기준, 재시작에도 유지·동시요청 중복호출 방지)
    camera_pricing.py          USD 기준 원가/마진 계산 (하드코딩 공식, 관세 로직 없음)
    camera_links.py             플랫폼별 국가/지역 구매 링크 생성
    camera_store.py             처리된 매물 인메모리 캐시 (/signals가 조회)
    camera_pipeline.py         raw → Gemini+Claude 캐시드 분석 → USD 원가 연산 → 발송/프리미엄/지연 판정
    camera_scheduler.py         5분마다 이베이 RSS 수집 → 파이프라인 실행 (백그라운드 루프)
    camera_mock.py             [개발/QA 전용] Mock 매물 생성 (양품/스캠/무료티어×2/프리미엄티어)
  api/
    routes.py                  /health, /scrape/{source}, /items
    camera_routes.py            /api/v1/test-feed(Mock), /signals, /free-signals, /premium-signals(실데이터)
outbox/                      수집 결과가 JSONL로 쌓이는 폴더 (다음 프로세스의 입력)
logs/                        pipeline.log (로테이팅 파일 로그)
dashboard/
  index.html                 실시간 시그널 대시보드 (싱글 파일, Tailwind CDN + Vanilla JS)
cache.db                     AI 응답 SQLite 캐시 (런타임에 생성됨, git에는 안 올라감)
Dockerfile                  프로덕션 컨테이너 빌드 정의
.dockerignore                 .env 등 이미지에 넣으면 안 되는 파일 제외 목록
```

## 설치 및 실행

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env          # 필요시 값 수정

uvicorn app.main:app --reload
```

브라우저에서 http://127.0.0.1:8000/docs 로 접속하면 Swagger UI로 바로 테스트할 수 있다.

## 동작 확인

```bash
# 헬스 체크
curl http://127.0.0.1:8000/health

# 이베이 소스 스크레이핑 실행 (USE_MOCK_DATA=true면 목 데이터 사용)
curl -X POST http://127.0.0.1:8000/scrape/ebay

# 최근 수집된 데이터 조회
curl http://127.0.0.1:8000/items
```

실행하면 `logs/pipeline.log`에 수집 로그가 쌓이고, `outbox/ebay_YYYYMMDD.jsonl`에
정형화된 데이터가 한 줄씩(JSON Lines) 적재된다.

## 실제 타깃 사이트 연동 방법

1. `.env`의 `USE_MOCK_DATA=false`, `TARGET_BASE_URL`을 실제 대상 URL로 설정.
2. `app/scrapers/ebay_mock.py`의 `EbayScraper.parse()` 안 CSS 선택자
   (`.product-card`, `.product-name` 등)를 실제 페이지 구조에 맞게 교체.
   RSS 피드라면 BeautifulSoup의 `xml` 파서로 바꾸거나 `feedparser`를 추가해도 된다.
3. 새로운 마켓(야후옥션 등)을 추가하려면:
   - `app/scrapers/`에 `BaseScraper`를 상속한 새 클래스를 만들고
   - `app/services/pipeline.py`의 `SCRAPER_REGISTRY`에 한 줄 등록.

## 다음 단계 (AI API 연동 시)

`outbox/*.jsonl`을 읽어 `ProductItem`으로 역직렬화하면 그대로 AI 분석
(번역, 카테고리 분류, 가격 적정성 판단 등) 단계의 입력으로 쓸 수 있다.
`ProductItem.extra` 필드는 이때 필요한 부가 정보를 위해 비워둔 확장 필드다.

## 카메라/렌즈 도메인 (전 세계 리셀러 대상 USD 기준 대시보드)

'하이엔드 빈티지/프리미엄 카메라 및 렌즈'를 타깃으로, 전 세계 리셀러가 쓰는
달러($) 기준 실시간 대시보드 API다. 모든 마진 연산은 USD로 통일되어 있고,
국가별 관세/부가세 로직은 없다 (국가마다 달라 리셀러가 알아서 처리하는 영역).
`app/schemas/camera.py`:

- `RawCameraItem` : 스크레이퍼가 그대로 긁어온 원본 데이터
  (상품ID, 상품명, URL, 원본 통화, 플랫폼, 해외판매가, 판매자평점/거래수, 본문, 이미지 배열).
- `ProcessedCameraItem` : `raw`(RawCameraItem 전체) + `ai_analysis`(상태등급/점수/결함/
  스캠위험도/위험도 근거 태그) + `pricing`(원본가·환율·USD환산가·배송비·수입원가·기준가·
  순수익·마진율까지 전부 노출) + `purchase_links`(국가/지역별 구매 링크)
  + `is_trigger` + `is_premium`.

```bash
# Mock 매물 5건(양품 / Junk 스캠 / 무료티어(공개) / 프리미엄티어 / 무료티어(지연중))을
# 파이프라인에 태운 결과 확인
curl http://127.0.0.1:8000/api/v1/test-feed

# 무료+프리미엄 전체 시그널 피드 (is_trigger=True만, 마진율 내림차순)
curl http://127.0.0.1:8000/api/v1/signals

# 무료 등급만 (is_trigger=True AND is_premium=False AND 발견된 지 30분 경과)
# — 로그인/결제 없이 접근 가능한 공개 API
curl http://127.0.0.1:8000/api/v1/free-signals

# 프리미엄 등급만 (is_trigger=True AND is_premium=True) — 지연 없이 실시간, 유료 멤버십 전용
curl http://127.0.0.1:8000/api/v1/premium-signals
```

- 라이카 M6 Mock (eBay, USD, 5분 전 발견): `Foreign_Buy_Price $1300 + Shipping $40 = $1340`,
  마진율 25%가 되도록 글로벌 기준가($1786.67)를 역산 → `is_trigger: true`,
  `is_premium: true`(마진 경로) → 방금 발견됐어도 프리미엄이라 지연 없이 즉시 노출.
- 후지필름 X100V Mock (Yahoo Auction, JPY 45,000 → USD $300 환산, 20분 전 발견): 표면
  마진율(66%)이 훨씬 높아도 본문에 `Junk`, `As-is`, `no returns`가 있어 스캠 위험도
  `High` 판정 → `is_trigger: false`로 억제되어 세 시그널 엔드포인트 어디에도 뜨지 않는다.
  (마진만 보고 사면 안 되는 함정 매물을 필터가 걸러내는지 검증하는 케이스)
- 니콘 FM2 Mock (eBay, USD, **45분 전** 발견): 마진 14.75% → `is_trigger: true`이지만
  마진<20%·점수<90라 `is_premium: false`. 발견된 지 30분이 지나 `/free-signals`에 노출됨.
- 콘탁스 T2 Mock (Mercari, JPY, 2분 전 발견): 마진 35% → `is_trigger: true`,
  `is_premium: true`(마진 경로). eBay 외 플랫폼(Mercari)·통화(JPY) 조합이면서, 방금
  발견됐어도 프리미엄이라 지연 없이 바로 `/premium-signals`에 노출.
- 올림푸스 OM-1 MD Mock (eBay, USD, **5분 전** 발견): 마진 12% → `is_trigger: true`,
  `is_premium: false`. FM2와 같은 무료 등급 자격이지만 발견된 지 30분이 안 지나
  `/free-signals`에는 아직 안 뜬다(전체 피드인 `/signals`에는 뜬다) — **30분 지연
  필터가 실제로 뭔가를 걸러내고 있음을 보여주는 핵심 케이스.**
- 세 엔드포인트 모두 `app/services/camera_store.py`의 인메모리 캐시를 조회만 한다.
  **더 이상 Mock으로 자동 시드하지 않는다** — 실제 값은 `app/services/
  camera_scheduler.py`의 백그라운드 루프(5분마다 이베이 RSS 수집)가 채운다.
  서버를 막 띄웠거나 이베이가 요청을 막은 직후에는 세 엔드포인트가 정직하게
  빈 리스트를 반환한다. `/api/v1/test-feed`는 여전히 Mock 5건을 쓰지만 개발/QA
  전용으로 분리됐다 (자세한 내용은 아래 "실제 수집기" 절 참고).
- **`/api/v1/premium-signals`에는 아직 인증/구독 검증이 없다.** 실서비스 전환 시
  API 키 또는 JWT 기반 멤버십 검증 미들웨어를 반드시 추가해야 한다.

### Freemium 등급 분류 (`app/services/camera_pipeline.py`)

```
is_trigger  = margin_rate_percent >= 10%  AND scam_risk != High
is_premium  = margin_rate_percent >= 20%  OR  condition_score >= 90
```

두 임계값을 다르게 둔 이유: `is_trigger`(10%)가 `is_premium`(20%)보다 낮아야
"시그널로 보여줄 만하지만 프리미엄까지는 아닌" 매물이 무료 티어에 남는다.
두 임계값이 같으면(예: 둘 다 20%) 트리거된 매물은 항상 프리미엄 조건도
충족해버려 무료 피드가 구조적으로 항상 비게 된다.

### 무료 티어 30분 지연 (`ProcessedCameraItem.discovered_at`)

`discovered_at`은 `raw.scraped_at`(스크레이핑 시각)을 그대로 물려받는다 — 이
매물이 시스템에 "처음 포착된" 시각이라는 개념 자체가 이미 스크레이핑 시각과
같기 때문에 별도의 최초-조회 시각 저장소를 새로 두지 않았다.

`/api/v1/free-signals`는 `now - discovered_at >= 30분`(`camera_pipeline.
FREE_TIER_DELAY`)인 매물만 통과시키고, `/api/v1/premium-signals`는 이 필터를
아예 타지 않아 발견 즉시 노출된다. AI 호출 자체는 (아래 캐싱 덕에) 발견
시점에 이미 끝나 있으므로, 이 지연은 비용 절감이 아니라 "즉시성"을 프리미엄
전용 가치로 남겨두기 위한 순수 노출 제어다.

### AI 응답 캐싱 — SQLite 파일 기반, 재시작에도 유지 (`app/services/ai_cache.py`)

Gemini/Claude 호출은 트래픽이 튈 때 비용이 그대로 폭탄이 되는 지점이라, 동일
매물(item_id 기준)에 대한 AI 분석은 **딱 한 번만** 실행되도록 캐싱했다.
처음에는 인메모리 dict였는데, 클라우드 배포 환경에서 컨테이너가 재시작되면
메모리가 통째로 사라져 이미 분석한 매물도 재시작 후 다시 과금되는 문제가
있어서 `cache.db`(SQLite, 표준 라이브러리 `sqlite3`만 사용, 추가 의존성 없음)
파일로 옮겼다.

```python
vision_result, scam_result = await asyncio.gather(
    get_or_compute(f"gemini:{raw.item_id}", lambda: analyze_condition_with_gemini(...), GeminiVisionResult),
    get_or_compute(f"claude:{raw.item_id}", lambda: analyze_scam_risk_with_claude(...), ClaudeScamResult),
)
```

- 테이블 스키마: `ai_cache(cache_key TEXT PRIMARY KEY, result_json TEXT, discovered_at TEXT)`.
  `result_json`은 `GeminiVisionResult`/`ClaudeScamResult` Pydantic 모델을 그대로
  `model_dump_json()`한 값이라, 캐시 히트 시 `result_type.model_validate_json()`으로
  원래 타입 그대로 복원된다.
- `sqlite3`는 동기(blocking) API라 이벤트 루프를 막지 않도록 모든 DB 접근을
  `asyncio.to_thread`로 스레드에 위임한다.
- 두 계층의 동시성 보호가 함께 필요하다: SQLite 자체는 "프로세스가 재시작돼도
  이미 계산한 결과가 남아있는지"를 보장하고, 키별 `asyncio.Lock` +
  double-checked locking은 "같은 프로세스 안에서 동시에 들어온 요청이 AI를
  중복 호출하지 않는지"를 보장한다 — 별개의 문제라 둘 다 필요하다.
- **실제로 두 개의 독립된 파이썬 프로세스**로 검증했다: 프로세스 A가 값을
  계산해 `cache.db`에 적재하고 완전히 종료된 뒤, 프로세스 B(서버 재시작을
  흉내)가 같은 키를 조회하니 `compute()`가 전혀 호출되지 않고 SQLite에서
  바로 값을 읽었다.
- **배포 시 주의**: `cache.db`는 컨테이너 로컬 디스크에 쓰인다. 컨테이너가
  재생성(재배포)되며 디스크까지 새로 만들어지는 환경(대부분의 컨테이너
  플랫폼 기본 동작)에서는 여전히 초기화된다. 재배포를 넘어서는 영구 보존이
  필요하면 `.env`의 `AI_CACHE_DB_PATH`를 퍼시스턴트 볼륨(Railway Volume 등)
  위의 경로로 설정해야 한다.
- `ai_cache.init_db()`는 `app/main.py`의 `lifespan`에서 기동 시 한 번 호출돼
  테이블을 준비한다. `ai_cache.clear()` / `ai_cache.cache_size()`는
  테스트·운영 확인용 헬퍼다(둘 다 `async def`).

### 국가별 구매 링크 (`app/services/camera_links.py`)

eBay는 전 세계 직접 구매/배송이 가능해 링크 하나(`GLOBAL`)로 충분하지만,
일본 야후옥션/메르카리는 일본 거주자가 아니면 직접 구매가 불가능해 Buyee,
ZenMarket 같은 대리구매(프록시 쇼핑) 서비스 링크를 함께 제공한다. 실제
서비스 적용 시 각 대리구매 업체의 공식 제휴 링크로 교체해야 한다.

### AI 서비스 레이어 (Gemini + Claude) — 전 세계 사용자 대상, 출력은 항상 영어

글로벌(All-English) 서비스로 피봇하면서 두 프롬프트 모두 한국어 출력을 유도하는
지침을 걷어내고 전부 영어로 다시 썼다. 입력 텍스트(본문)는 여전히 다국어를
인식하지만(야후옥션/메르카리는 실제로 일본어 원문일 수 있음), AI가 반환하는
모든 텍스트(등급명, 결함 라벨, 위험도 사유 태그)는 항상 영어다.

- `app/services/ai_gemini.py` : 이미지 URL 리스트를 다운로드해 Gemini Vision에
  전달, `response_schema`(Structured Output)로 `condition_grade`/`condition_score`/
  결함 플래그(lens fungus·water damage·scratches·dents)를 강제로 받는다. `notes`도
  영어로만 반환하도록 프롬프트에 명시했다.
- `app/services/ai_claude.py` : 본문+판매자 평점/거래수를 Claude에 전달,
  tool-use(강제 함수 호출) 방식으로 `scam_risk`(`High`/`Medium`/`Low`)와
  `risk_reasons`를 구조화해서 받는다. `risk_reasons`는 자유 텍스트가 아니라
  `STANDARD_RISK_TAGS`(예: `'As-Is / Parts Only'`, `'Defective LCD'`,
  `'Low Rating Seller'`)라는 고정 어휘에서만 고르도록 도구 스키마의 `enum`으로
  강제했다 — 다운스트림(대시보드 필터링, 분석)이 자유 텍스트가 아닌 고정 태그에
  의존할 수 있게 하기 위함이다. `AIAnalysis.scam_risk_reasons` 필드로 응답에 노출된다.
- 두 함수 모두 `GEMINI_API_KEY`/`CLAUDE_API_KEY`가 비어 있거나 호출이 실패하면
  키워드+판매자 신뢰도 기반의 규칙 기반 fallback으로 자동 대체된다 — AI API 장애
  하나로 무인 파이프라인 전체가 멈추지 않도록 하는 안전장치다. fallback도 동일하게
  `STANDARD_RISK_TAGS`의 영어 태그만 반환한다(키워드 인식 자체는 다국어 유지).
- `.env`에 실제 API 키를 채우면 다음 실행부터 자동으로 실제 AI 호출로 전환된다
  (코드 수정 불필요).

### USD 기준 마진 연산 (`app/services/camera_pricing.py`)

```
Foreign_Buy_Price (USD) = 원본가 * 환율(JPY만; USD는 1.0)
Final_Import_Cost (USD) = Foreign_Buy_Price + Global_Shipping_Fee($40 고정)
Net_Profit (USD)        = Global_Baseline_Price - Final_Import_Cost
Margin_Rate (%)         = Net_Profit / Global_Baseline_Price * 100
```

JPY→USD 환율은 `.env`(`FX_RATE_JPY_USD`, 기본 150엔=$1)에서 읽는다.
배송비(`GLOBAL_SHIPPING_FEE_USD`)도 `.env`로 조정 가능하다.

### 실제 수집기: 이베이 RSS + 백그라운드 스케줄러

`app/scrapers/ebay_rss.py`가 Mock을 대체하는 실제 데이터 소스다. 일반 검색
결과 HTML은 Cloudflare/봇 탐지에 쉽게 막히므로, 이베이가 공식 제공하는
RSS 피드(`https://www.ebay.com/sch/i.html?_nkw=<검색어>&_rss=1`)를 대신
쓴다. `feedparser`로 파싱해서 `RawCameraItem`으로 매핑한다.

```bash
uvicorn app.main:app --reload
# 기동 즉시 첫 수집 사이클이 돌고, 이후 5분(CAMERA_SCRAPE_INTERVAL_SECONDS)마다 반복된다.
# 결과는 /api/v1/signals, /free-signals, /premium-signals에 쌓인다.
```

- **대상 키워드**는 `app/services/camera_scheduler.py`의
  `KEYWORD_BASELINE_PRICE_USD` 딕셔너리 키로 정의한다 (현재 `leica m6`,
  `contax t2`, `nikon fm2`). 같은 딕셔너리의 값이 그 키워드의 글로벌
  기준가(USD)다 — 아직 실시간 시세 API가 없어 사람이 채운 참고값이며,
  키워드를 추가/삭제하면 수집 대상도 함께 바뀐다.
- **RSS의 한계 (정직하게 문서화)**: `<description>`은 판매자가 쓴 본문 전체가
  아니라 가격/썸네일 위주의 짧은 요약이고, 이미지도 보통 1장뿐이다. 판매자
  평점(`seller_rating`)·거래수(`seller_transaction_count`)는 RSS에 아예 없어
  `RawCameraItem`에서 `Optional`로 바꾸고 `None`으로 채운다 —
  `ai_claude.py`는 `None`을 "판매자 신뢰도 미검증"으로 취급해 `New/Unverified
  Seller` 태그와 함께 최소 `Medium` 위험도로 판정한다. 진짜 본문 전체·전체
  이미지 갤러리·판매자 통계가 필요하면 eBay Browse API(OAuth) 연동이 필요하다.
- **차단 시 우아하게 대응**: 이 프로젝트를 개발한 네트워크 환경에서는 실제로
  이 RSS 엔드포인트가 403으로 막히는 것을 확인했다 — "RSS라서 안전하다"는
  보장은 없다. `fetch_ebay_rss_items()`는 네트워크/파싱 실패 시 예외를 삼키고
  빈 리스트를 반환하며, 스케줄러는 한 키워드가 실패해도 나머지 키워드는 계속
  진행한다. 배포 환경(주거용 IP, 프록시 등)에서는 통과할 수 있다.
- **스케줄러 On/Off**: `.env`의 `CAMERA_SCHEDULER_ENABLED=false`로 끄면 백그라운드
  수집 없이 `/api/v1/test-feed`(Mock)만으로 파이프라인 로직을 확인할 수 있다.
  간격은 `CAMERA_SCRAPE_INTERVAL_SECONDS`(기본 300초)로 조정한다.
- `app/main.py`의 `lifespan`이 `asyncio.create_task(scrape_loop())`로 백그라운드
  루프를 시작/종료한다 (FastAPI가 권장하는 방식 — 구버전 `@app.on_event`
  데코레이터는 쓰지 않았다).

## 실시간 대시보드 (`dashboard/index.html`)

전 세계 리셀러 대상 Dark Premium 테마의 싱글 파일 대시보드 MVP. 순수
HTML + Tailwind CDN + Vanilla JS로만 되어 있어 빌드 도구 없이 그대로 연다.

```bash
uvicorn app.main:app --reload
# http://127.0.0.1:8000/dashboard/ 접속
# → 같은 오리진이라 fetch("/api/v1/signals")가 별도 설정 없이 바로 붙는다
```

- 백엔드 없이 `dashboard/index.html`을 더블클릭해서 열어도 동작한다. `fetch`가
  실패하면(백엔드 미기동, CORS, 파일 직접 열기 등) 자동으로 **Mock 데이터**
  (라이카 M6 트리거 매물 + X100V 스캠 필터 매물)로 폴백하고 상태 배지가
  `MOCK DATA`(호박색)로 표시된다. 백엔드가 붙으면 `LIVE`(네온 라임)로 바뀐다.
- `CONFIG.API_BASE`(스크립트 상단)를 바꾸면 다른 호스트의 백엔드를 가리킬 수 있다
  (예: 프론트만 별도 정적 호스팅에 배포하는 경우). 이 경우를 대비해 백엔드에
  CORS(`app/main.py`)도 전체 허용으로 열어뒀다 — 실서비스 전에는 도메인을 제한해야 한다.
- 20초 간격으로 `/api/v1/signals`를 폴링하고, 상단 정렬 탭(Margin ↓ / Newest)은
  클라이언트에서 즉시 재정렬한다. 헤더의 FX 스트립(USD/JPY 등)은 실시간 시세
  API와 연동되지 않은 참고용 표시 값이다.
- 카드에 필요한 `ai_analysis.condition_score`(Gemini 0~100점)는 이번에 백엔드
  스키마에 추가된 필드다 — 기존에는 계산은 하고도 응답에 담지 않던 누락을 함께 고쳤다.
- **아직 업데이트 전:** 대시보드는 여전히 `/api/v1/signals`(무료+프리미엄 통합)를
  호출한다. Freemium 분리 이후 `is_premium` 배지 표시나 `/free-signals` vs
  `/premium-signals` 전환 UI는 프론트엔드 쪽에서 별도로 반영해야 한다.

## Docker 배포

```bash
docker build -t arbitrage-backend .
docker run -p 8000:8000 --env-file .env arbitrage-backend
```

- `Dockerfile`은 `python:3.12-slim` 베이스에 `requirements.txt`를 코드보다
  먼저 복사해서(레이어 캐시), 코드만 바뀌는 재빌드에서 `pip install`을
  건너뛴다. 루트가 아닌 사용자(`appuser`)로 실행하고, `/health`를 찌르는
  `HEALTHCHECK`이 붙어 있다.
- `.dockerignore`가 `.env`(실제 API 키 포함), `.venv/`, `cache.db`,
  `logs/`, `outbox/`를 이미지에서 제외한다 — **`.env`가 이미지 레이어에
  구워지면 레지스트리에 푸시하는 순간 키가 유출된다**는 점이 이 프로젝트
  진행 중 실제로 우려됐던 부분이라 특히 신경 써서 뺐다.
- **`cache.db`는 컨테이너 로컬 디스크에 쓰인다.** 재배포 시 디스크까지 새로
  만들어지는 환경(대부분의 컨테이너 플랫폼 기본값)에서는 캐시가 초기화된다.
  재배포를 넘어서는 영구 캐시가 필요하면 퍼시스턴트 볼륨을 마운트하고
  `AI_CACHE_DB_PATH`로 그 안의 경로를 가리키게 할 것:
  ```bash
  docker run -v arbitrage-cache:/app/data \
             -e AI_CACHE_DB_PATH=/app/data/cache.db \
             -p 8000:8000 --env-file .env arbitrage-backend
  ```
- 이 개발 환경에는 Docker CLI가 없어 `docker build`를 직접 실행해 검증하지는
  못했다 — `COPY`하는 경로(`requirements.txt`, `app/`, `dashboard/`)가 실제로
  존재하는지, `requirements.txt`의 모든 패키지가 이번 세션에서 실제 설치·
  사용됐는지는 정적으로 확인했다. 실제 배포 전에 로컬에서 한 번 빌드해보길
  권한다.
