"""
API 라우트 정의.

엔드포인트는 최소한만 둔다:
- GET  /health          : 헬스 체크
- POST /scrape/{source} : 등록된 소스 하나를 즉시 스크레이핑 실행
- GET  /items           : 최근 수집된 데이터 조회 (메모리 캐시 기반)
"""
from fastapi import APIRouter, HTTPException

from app.core.logger import get_logger
from app.schemas.product import ProductItem, ScrapeJobResult
from app.services.pipeline import SCRAPER_REGISTRY, get_recent_items, run_pipeline

logger = get_logger(__name__)
router = APIRouter()


@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@router.post("/scrape/{source}", response_model=ScrapeJobResult)
async def trigger_scrape(source: str) -> ScrapeJobResult:
    """지정한 소스(예: 'ebay')에 대해 스크레이핑 파이프라인을 실행한다."""
    if source not in SCRAPER_REGISTRY:
        raise HTTPException(
            status_code=404,
            detail=f"등록되지 않은 소스입니다: {source!r}. 사용 가능: {list(SCRAPER_REGISTRY)}",
        )

    try:
        return await run_pipeline(source)
    except Exception as exc:  # noqa: BLE001 - API 경계에서는 원인을 그대로 노출하지 않는다
        logger.exception("[%s] 파이프라인 실행 중 오류", source)
        raise HTTPException(status_code=502, detail="스크레이핑 파이프라인 실행에 실패했습니다.") from exc


@router.get("/items", response_model=list[ProductItem])
async def list_recent_items(limit: int = 50) -> list[ProductItem]:
    """가장 최근에 수집된 데이터를 반환한다 (재시작 시 초기화되는 메모리 캐시)."""
    return get_recent_items(limit=limit)
