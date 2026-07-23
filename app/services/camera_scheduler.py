"""
카메라/렌즈 백그라운드 수집 스케줄러.

FastAPI lifespan(app/main.py)에서 백그라운드 asyncio 태스크로 시작된다.
설정된 각 검색 키워드에 대해 이베이 RSS를 수집 → (캐싱된) AI 분석 →
USD 원가/마진 연산 → camera_store 적재까지 한 사이클로 묶어서,
`camera_scrape_interval_seconds`(기본 5분) 간격으로 무한 반복한다.

`/api/v1/free-signals`, `/api/v1/premium-signals`는 이 스케줄러가 채워 넣는
camera_store를 조회만 한다 — Mock 데이터로 자동 시드하던 이전 동작은
제거했다(더 이상 라우트에서 Mock을 참조하지 않는다).
"""
import asyncio

from app.core.config import settings
from app.core.logger import get_logger
from app.scrapers.ebay_rss import fetch_ebay_rss_items
from app.services.camera_pipeline import process_raw_camera_item

logger = get_logger(__name__)

# 검색 키워드 → 글로벌 기준가(USD) 정적 lookup.
# 실시간 시세 API가 아직 없어 사람이 채운 참고값이며, 스크레이핑 대상
# 키워드 목록도 이 딕셔너리의 키로 정의된다 (키를 추가/삭제하면 대상도 바뀐다).
KEYWORD_BASELINE_PRICE_USD = {
    "leica m6": 3200.0,
    "contax t2": 950.0,
    "nikon fm2": 320.0,
}


async def run_scrape_cycle() -> int:
    """설정된 모든 키워드에 대해 한 번씩 수집~파이프라인 처리를 수행한다.

    한 키워드가 실패(네트워크 오류, 파싱 실패 등)해도 나머지 키워드는 계속
    진행한다. 반환값은 이번 사이클에서 성공적으로 처리한 매물 수.
    """
    total_processed = 0

    for keyword, baseline_price_usd in KEYWORD_BASELINE_PRICE_USD.items():
        try:
            raw_items = await fetch_ebay_rss_items(keyword)
        except Exception:
            logger.exception("[scheduler] '%s' RSS 수집 중 처리되지 않은 오류, 건너뜀", keyword)
            continue

        if not raw_items:
            logger.info("[scheduler] '%s' 수집 결과 0건 (차단/변경 가능성)", keyword)
            continue

        results = await asyncio.gather(
            *(process_raw_camera_item(item, baseline_price_usd) for item in raw_items),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("[scheduler] '%s' 매물 하나 처리 실패: %s", keyword, result)
            else:
                total_processed += 1

    logger.info("[scheduler] 수집 사이클 완료: %d건 처리", total_processed)
    return total_processed


async def scrape_loop() -> None:
    """camera_scrape_interval_seconds 간격으로 run_scrape_cycle을 반복하는 백그라운드 루프.

    기동 직후 첫 사이클을 바로 실행하고(대시보드가 곧장 데이터를 보게), 그 뒤부터
    간격을 둔다. 개별 사이클에서 예외가 새어나와도 루프 자체는 죽지 않는다.
    """
    logger.info(
        "[scheduler] 카메라 수집 루프 시작 (간격 %d초, 대상 키워드: %s)",
        settings.camera_scrape_interval_seconds,
        list(KEYWORD_BASELINE_PRICE_USD),
    )
    while True:
        try:
            await run_scrape_cycle()
        except Exception:
            logger.exception("[scheduler] 수집 사이클 중 처리되지 않은 오류")
        await asyncio.sleep(settings.camera_scrape_interval_seconds)
