"""
카메라/렌즈 매물 처리 오케스트레이터.

RawCameraItem 하나를 받아
  (1) Gemini 비전 분석(이미지 → 외관 상태) + Claude 텍스트 분석(본문/판매자 → 스캠 위험도)을
      병렬로 호출하고,
  (2) 그 결과와 가격 정보로 USD 기준 원가/마진을 연산하고,
  (3) 마진율과 스캠 위험도를 기준으로 시그널 트리거 여부(is_trigger)를,
      마진율/AI 상태 점수를 기준으로 프리미엄 등급(is_premium)을 판정하고,
  (4) 플랫폼에 맞는 국가별 구매 링크를 붙여서
최종 ProcessedCameraItem을 만든다. 실제 스크레이핑 파이프라인과 Mock 테스트 피드가
모두 이 함수 하나만 호출하면 되도록 진입점을 통일했다. 처리 결과는 camera_store에
캐시되어 `/api/v1/signals`, `/api/v1/free-signals`, `/api/v1/premium-signals`가 조회한다.

Gemini/Claude 호출은 ai_cache.get_or_compute()로 감싸서 item_id 기준으로
캐싱한다 — 동일한 매물을 여러 요청이 동시에 처리하더라도, 그리고 서버가
재시작되더라도(SQLite 파일 캐시) AI API는 최초 1회만 불린다. (자세한 내용은
app/services/ai_cache.py 참고)
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.logger import get_logger
from app.schemas.camera import (
    AIAnalysis,
    ConditionGrade,
    PricingBreakdown,
    ProcessedCameraItem,
    RawCameraItem,
    ScamRiskLevel,
)
from app.services.ai_cache import get_or_compute
from app.services.ai_claude import ClaudeScamResult, analyze_scam_risk_with_claude
from app.services.ai_gemini import DEFECT_FIELD_LABELS, GeminiVisionResult, analyze_condition_with_gemini
from app.services.camera_links import build_purchase_links
from app.services.camera_pricing import (
    calculate_final_import_cost_usd,
    calculate_margin_usd,
    convert_to_usd,
    get_exchange_rate_to_usd,
)
from app.services.camera_store import cache_processed_item

logger = get_logger(__name__)

# 이 마진율(%) 이상이어야 구매 시그널을 보낼 가치가 있다고 판단하는 임계값.
# is_premium 임계값(20%)보다 낮게 잡아, "시그널로는 보여줄 만하지만 프리미엄까지는
# 아닌" 매물이 무료 티어(free-signals)에 남을 여지를 만든다 (프리미엄 전용 필터는
# 아래 determine_is_premium 참고).
MIN_TRIGGER_MARGIN_PERCENT = 10.0

# 프리미엄(유료) 등급 임계값. 마진율 또는 AI 상태 점수 둘 중 하나만 넘어도 프리미엄.
MIN_PREMIUM_MARGIN_PERCENT = 20.0
MIN_PREMIUM_CONDITION_SCORE = 90


def _build_ai_analysis(vision_result: GeminiVisionResult, scam_result: ClaudeScamResult) -> AIAnalysis:
    """Gemini(외관)와 Claude(스캠) 결과를 하나의 AIAnalysis로 합친다."""
    detected_defects = [
        label for field_name, label in DEFECT_FIELD_LABELS.items() if getattr(vision_result, field_name)
    ]
    return AIAnalysis(
        condition_grade=ConditionGrade(vision_result.condition_grade),
        condition_score=vision_result.condition_score,
        detected_defects=detected_defects,
        scam_risk=ScamRiskLevel(scam_result.scam_risk),
        scam_risk_reasons=scam_result.risk_reasons,
    )


def determine_is_trigger(margin_rate_percent: float, scam_risk: ScamRiskLevel) -> bool:
    """마진율 10% 이상 + scam_risk가 High가 아닐 때만 True.

    스캠 위험도가 High면 마진이 아무리 좋아도 시그널을 보내지 않는다
    (마진만 보고 사면 안 되는 함정 매물을 걸러내기 위함).
    """
    if scam_risk == ScamRiskLevel.HIGH:
        return False
    return margin_rate_percent >= MIN_TRIGGER_MARGIN_PERCENT


def determine_is_premium(margin_rate_percent: float, condition_score: int) -> bool:
    """마진율 20% 이상이거나 AI 상태 점수 90점 이상이면 프리미엄(유료) 등급."""
    return margin_rate_percent >= MIN_PREMIUM_MARGIN_PERCENT or condition_score >= MIN_PREMIUM_CONDITION_SCORE


# 무료 티어는 발견 즉시가 아니라 이만큼 지난 뒤에야 노출된다. 프리미엄은 이 지연이 없다.
FREE_TIER_DELAY = timedelta(minutes=30)


def is_visible_in_free_tier(discovered_at: datetime, now: Optional[datetime] = None) -> bool:
    """discovered_at으로부터 FREE_TIER_DELAY(기본 30분) 이상 지났을 때만 True."""
    now = now or datetime.now(timezone.utc)
    return (now - discovered_at) >= FREE_TIER_DELAY


async def process_raw_camera_item(raw: RawCameraItem, global_baseline_price_usd: float) -> ProcessedCameraItem:
    """원본 매물 데이터를 AI 분석 + USD 원가 연산해서 최종 송출용 데이터로 변환한다.

    global_baseline_price_usd(글로벌 기준가, USD)는 아직 글로벌 시세 자동 조회가
    붙지 않아 외부에서 주입받는다. (추후 실시간 시세 API로 대체 예정)
    """
    # item_id 기준으로 캐싱 — 같은 매물을 여러 요청이 동시에 처리해도, 서버가
    # 재시작돼도(SQLite) 실제 AI 호출(compute)은 단 한 번만 실행된다.
    vision_result, scam_result = await asyncio.gather(
        get_or_compute(
            f"gemini:{raw.item_id}",
            lambda: analyze_condition_with_gemini(raw.image_urls),
            GeminiVisionResult,
        ),
        get_or_compute(
            f"claude:{raw.item_id}",
            lambda: analyze_scam_risk_with_claude(
                raw.description, raw.seller_rating, raw.seller_transaction_count
            ),
            ClaudeScamResult,
        ),
    )
    ai_analysis = _build_ai_analysis(vision_result, scam_result)

    exchange_rate = get_exchange_rate_to_usd(raw.currency)
    foreign_buy_price_usd = convert_to_usd(raw.price, raw.currency)
    final_import_cost_usd = calculate_final_import_cost_usd(foreign_buy_price_usd)
    net_profit_usd, margin_rate_percent = calculate_margin_usd(
        global_baseline_price_usd, final_import_cost_usd
    )

    pricing = PricingBreakdown(
        original_price=raw.price,
        original_currency=raw.currency,
        exchange_rate_to_usd=exchange_rate,
        foreign_buy_price_usd=round(foreign_buy_price_usd, 2),
        global_shipping_fee_usd=round(final_import_cost_usd - foreign_buy_price_usd, 2),
        final_import_cost_usd=round(final_import_cost_usd, 2),
        global_baseline_price_usd=global_baseline_price_usd,
        net_profit_usd=net_profit_usd,
        margin_rate_percent=margin_rate_percent,
    )

    is_trigger = determine_is_trigger(margin_rate_percent, ai_analysis.scam_risk)
    is_premium = determine_is_premium(margin_rate_percent, ai_analysis.condition_score)
    purchase_links = build_purchase_links(raw)

    logger.info(
        "[camera] %s | grade=%s | risk=%s | margin=%.2f%% | is_trigger=%s | is_premium=%s",
        raw.name,
        ai_analysis.condition_grade.value,
        ai_analysis.scam_risk.value,
        margin_rate_percent,
        is_trigger,
        is_premium,
    )

    processed = ProcessedCameraItem(
        raw=raw,
        ai_analysis=ai_analysis,
        pricing=pricing,
        purchase_links=purchase_links,
        discovered_at=raw.scraped_at,
        is_trigger=is_trigger,
        is_premium=is_premium,
    )
    cache_processed_item(processed)
    return processed
