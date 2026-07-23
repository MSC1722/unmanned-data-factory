"""
파이프라인 오케스트레이터.

흐름: 스크레이퍼 실행 → (Pydantic으로 이미 검증된) 데이터 로깅 → outbox에 적재.

outbox/*.jsonl 파일이 이 단계와 "다음 프로세스"(추후 붙일 AI 분석/가공 단계)
사이의 경계다. 지금은 AI API가 없으므로 여기서 멈추고, 다음 단계는 이 JSONL을
읽어가기만 하면 되도록 설계했다.
"""
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

from app.core.config import settings
from app.core.logger import get_logger
from app.schemas.product import ProductItem, ScrapeJobResult
from app.scrapers.base import BaseScraper
from app.scrapers.ebay_mock import EbayScraper

logger = get_logger(__name__)

# 소스 이름 → 스크레이퍼 클래스 매핑. 새 소스(야후옥션 등)를 추가할 때는
# 여기에 한 줄만 등록하면 API/파이프라인 코드를 건드릴 필요가 없다.
SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "ebay": EbayScraper,
}

# API의 GET /items 가 참조하는 최근 수집 결과 캐시.
# 아직 DB를 붙이기 전이라 메모리에만 보관하며, 재시작하면 사라진다.
# 영구 기록은 outbox/*.jsonl 파일이 담당한다.
_recent_items: list[ProductItem] = []
_MAX_RECENT_ITEMS = 500


def get_recent_items(limit: int = 50) -> list[ProductItem]:
    return _recent_items[-limit:]


async def _handoff_to_outbox(source: str, items: list[ProductItem]) -> Path:
    """검증이 끝난 데이터를 outbox 디렉터리에 JSONL로 적재한다.

    다음 프로세스(예: AI 기반 번역/카테고리 분류/가격 분석)는 이 파일을
    한 줄씩 읽어 ProductItem으로 재구성해서 이어받으면 된다.
    """
    outbox_dir = Path(settings.outbox_dir)
    outbox_dir.mkdir(exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    outbox_path = outbox_dir / f"{source}_{date_str}.jsonl"

    async with aiofiles.open(outbox_path, mode="a", encoding="utf-8") as f:
        for item in items:
            await f.write(item.model_dump_json() + "\n")

    logger.info("[%s] outbox 적재 완료: %s (%d건)", source, outbox_path, len(items))
    return outbox_path


async def run_pipeline(source: str) -> ScrapeJobResult:
    """소스 하나에 대해 스크레이핑 → 로깅 → 적재를 실행하고 결과 요약을 반환한다."""
    scraper_cls = SCRAPER_REGISTRY.get(source)
    if scraper_cls is None:
        raise ValueError(
            f"등록되지 않은 소스: {source!r} (등록된 소스: {list(SCRAPER_REGISTRY)})"
        )

    scraper = scraper_cls()
    items = await scraper.run()

    for item in items:
        logger.info(
            "[%s] 수집됨 | name=%s | price=%s %s | images=%d",
            source,
            item.name,
            item.price,
            item.currency,
            len(item.image_urls),
        )

    await _handoff_to_outbox(source, items)

    # 메모리 캐시 갱신 (API 조회용, 최근 N건만 유지)
    _recent_items.extend(items)
    del _recent_items[: max(0, len(_recent_items) - _MAX_RECENT_ITEMS)]

    return ScrapeJobResult(source=source, item_count=len(items), items=items)
