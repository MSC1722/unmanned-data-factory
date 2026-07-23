"""
카메라/렌즈 도메인 API 라우트.

- GET /api/v1/test-feed        : [개발/QA 전용] Mock 데이터 5건(양품/스캠/무료티어×2/
  프리미엄티어)을 파이프라인에 태워 반환한다. 실제 시그널 피드와는 완전히 분리돼
  있다 — 더 이상 free/premium-signals의 데이터 소스로 쓰이지 않는다
  (네트워크가 없거나 AI 키가 없는 환경에서도 파이프라인 로직만 확인하고 싶을 때 쓴다).
- GET /api/v1/signals          : is_trigger=True 전체(무료+프리미엄) 실시간 피드.
- GET /api/v1/free-signals     : is_trigger=True 이면서 is_premium=False이고, 발견된 지
  30분(FREE_TIER_DELAY) 이상 지난 매물만 반환하는 무료 피드. 로그인/결제 없이도
  접근 가능한 공개 API — 구글 애드센스 크롤러 등도 대상. "즉시성"을 프리미엄
  전용 가치로 남겨두기 위한 지연 로직이 여기 있다.
- GET /api/v1/premium-signals  : is_trigger=True 이면서 is_premium=True(마진 20%↑)인
  매물을 지연 없이 발견 즉시 반환하는 실시간 프리미엄 피드.
  NOTE: 아직 실제 인증/구독 검증 미들웨어는 없다 — 프로덕션 전환 시 이 라우트 앞에
  API 키/JWT 등 멤버십 검증을 반드시 추가해야 한다.

signals/free-signals/premium-signals 세 엔드포인트는 camera_store의 캐시를 조회만
한다 — 실제 값 채우기는 app/services/camera_scheduler.py의 백그라운드 루프(5분 간격
이베이 RSS 수집)가 전담한다. 서버가 막 기동했거나 이베이가 요청을 막은 직후에는
이 세 엔드포인트가 빈 리스트를 반환할 수 있다(정상 동작 — 더 이상 Mock으로
채워서 감추지 않는다).

세 시그널 엔드포인트 모두 margin_rate_percent 내림차순으로 정렬해서 반환한다.
AI 호출 비용은 이 라우트가 아니라 app/services/ai_cache.py의 캐싱으로 억제된다
(같은 매물의 Gemini/Claude 분석은 파이프라인 진입 시점에 최초 1회만 실행).
"""
from typing import Callable

from fastapi import APIRouter

from app.core.logger import get_logger
from app.schemas.camera import CameraTestFeedResponse, ProcessedCameraItem, SignalsResponse
from app.services.camera_mock import get_mock_test_feed
from app.services.camera_pipeline import is_visible_in_free_tier
from app.services.camera_store import get_all_processed_items

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["camera"])


@router.get("/test-feed", response_model=CameraTestFeedResponse)
async def get_test_feed() -> CameraTestFeedResponse:
    """[개발/QA 전용] Mock 매물을 파이프라인에 태워 반환한다.

    실제 시그널 엔드포인트(signals/free-signals/premium-signals)와는 분리돼 있고,
    이 호출은 camera_store 캐시에도 함께 쌓인다(실제 스크레이핑 결과와 동일한
    경로를 타므로) — 로컬에서 프론트를 확인할 때는 이 엔드포인트를 한 번 호출해
    캐시를 채워도 된다.
    """
    items = await get_mock_test_feed()
    logger.info("[camera] test-feed 요청 처리 (%d건)", len(items))
    return CameraTestFeedResponse(item_count=len(items), items=items)


def _get_signals(label: str, predicate: Callable[[ProcessedCameraItem], bool]) -> SignalsResponse:
    """camera_store 캐시에서 is_trigger=True + predicate를 만족하는 매물만
    마진율 내림차순으로 뽑아 SignalsResponse로 감싼다. (캐시가 비어 있으면
    빈 리스트를 그대로 반환한다 — 더 이상 Mock으로 자동 시드하지 않는다.)"""
    all_items = get_all_processed_items()
    filtered = sorted(
        (item for item in all_items if item.is_trigger and predicate(item)),
        key=lambda item: item.pricing.margin_rate_percent,
        reverse=True,
    )
    logger.info("[camera] %s 요청 처리 (전체 %d건 중 %d건)", label, len(all_items), len(filtered))
    return SignalsResponse(signal_count=len(filtered), signals=filtered)


@router.get("/signals", response_model=SignalsResponse)
async def get_signals() -> SignalsResponse:
    """실시간 시그널 피드 (무료+프리미엄 전체). is_trigger=True인 매물만 반환한다."""
    return _get_signals("signals", lambda item: True)


@router.get("/free-signals", response_model=SignalsResponse)
async def get_free_signals() -> SignalsResponse:
    """무료 등급 시그널 피드. 로그인/결제 없이 접근 가능한 공개 API.

    발견된 지 30분이 안 된 매물은 아직 보여주지 않는다 — AI 호출 자체는
    (캐싱 덕에) 발견 시점에 이미 1회로 끝나 있지만, "즉시성"을 프리미엄만의
    가치로 남겨두기 위한 노출 지연이다.
    """
    return _get_signals(
        "free-signals",
        lambda item: not item.is_premium and is_visible_in_free_tier(item.discovered_at),
    )


@router.get("/premium-signals", response_model=SignalsResponse)
async def get_premium_signals() -> SignalsResponse:
    """프리미엄(유료 멤버십) 시그널 피드. 지연 없이 발견 즉시 반환한다.

    NOTE: 아직 인증/구독 검증이 붙어있지 않다. 실서비스 전환 시 이 라우트에
    API 키 또는 JWT 기반 멤버십 검증 의존성을 추가해야 한다.
    """
    return _get_signals("premium-signals", lambda item: item.is_premium)
