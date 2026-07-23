"""
Claude(Anthropic) API를 이용한 판매자 정보/본문 텍스트 기반 스캠 위험도 분석.

Anthropic 공식 SDK(anthropic)의 tool-use(강제 함수 호출) 방식으로
Structured Output을 받는다 — Claude는 Gemini의 response_schema 같은 전용
구조화 출력 파라미터가 없으므로, JSON을 신뢰성 있게 받는 표준 방법인
"도구 하나를 정의하고 그 도구 호출을 강제"하는 패턴을 쓴다.

CLAUDE_API_KEY가 비어 있거나 호출이 실패하면 키워드 + 판매자 신뢰도 기반의
규칙 기반 fallback으로 대체한다.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

_SCAM_ANALYSIS_SYSTEM_PROMPT = """You are an expert at detecting scam listings and predatory terms in
international second-hand marketplace listings. Analyze the given listing text and seller info,
then judge scam_risk.

Follow these rules strictly:
- If the listing text contains any predatory phrase — such as being sold "as-is", "for parts only",
  "junk", or with "no returns accepted" — scam_risk MUST be "High".
- Even without such phrases, a low seller rating or a low transaction count MUST push scam_risk to
  at least "Medium".
- If seller rating or transaction count is reported as "not available", treat the seller as
  unverified — this also pushes scam_risk to at least "Medium" (tag it "New/Unverified Seller").
- If none of the above signals are present, scam_risk is "Low".

risk_reasons MUST be an array of standardized English tags only — pick every tag that applies from
the STANDARD_RISK_TAGS enum provided in the tool schema. Never invent free-text reasons and never
output any language other than English.

Call the report_scam_risk tool with your structured result — do not respond in plain text."""

# Controlled vocabulary so downstream filtering/analytics can rely on a fixed tag set instead of
# free-text strings. Claude must pick from this list (enforced via the tool's JSON schema enum).
STANDARD_RISK_TAGS = [
    "As-Is / Parts Only",
    "No Returns Accepted",
    "Reported Non-Functional",
    "Low Rating Seller",
    "New/Unverified Seller",
    "Defective LCD",
    "Non-Functional Shutter",
    "Water Damage Reported",
    "Fungus/Mold Reported",
    "Vague Description",
]

_SCAM_RISK_TOOL = {
    "name": "report_scam_risk",
    "description": "Report the structured result of analyzing a listing's text and seller info.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scam_risk": {
                "type": "string",
                "enum": ["High", "Medium", "Low"],
                "description": "Scam / predatory-terms risk level",
            },
            "risk_reasons": {
                "type": "array",
                "items": {"type": "string", "enum": STANDARD_RISK_TAGS},
                "description": "Standardized English tags backing the scam_risk judgment",
            },
        },
        "required": ["scam_risk", "risk_reasons"],
    },
}


class ClaudeScamResult(BaseModel):
    """Claude tool-use 결과를 담는 스키마."""

    scam_risk: Literal["High", "Medium", "Low"]
    risk_reasons: list = Field(default_factory=list)


async def analyze_scam_risk_with_claude(
    description: str,
    seller_rating: Optional[float],
    seller_transaction_count: Optional[int],
) -> ClaudeScamResult:
    """본문 + 판매자 정보를 Claude에 넘겨 스캠 위험도를 판정한다.

    seller_rating/seller_transaction_count는 RSS 등 신뢰도 정보가 없는 소스에서
    수집된 경우 None일 수 있다.
    """
    if not settings.claude_api_key:
        logger.info("[claude] CLAUDE_API_KEY 미설정 → fallback 분석 사용")
        return _fallback_scam_analysis(description, seller_rating, seller_transaction_count)

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.claude_api_key)
        rating_text = f"{seller_rating}" if seller_rating is not None else "not available"
        count_text = f"{seller_transaction_count}" if seller_transaction_count is not None else "not available"
        user_message = (
            f"Listing text:\n{description}\n\n"
            f"Seller rating: {rating_text}\n"
            f"Seller transaction count: {count_text}"
        )
        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=1024,
            system=_SCAM_ANALYSIS_SYSTEM_PROMPT,
            tools=[_SCAM_RISK_TOOL],
            tool_choice={"type": "tool", "name": "report_scam_risk"},
            messages=[{"role": "user", "content": user_message}],
        )
        tool_use = next(block for block in response.content if block.type == "tool_use")
        return ClaudeScamResult.model_validate(tool_use.input)

    except Exception:
        logger.exception("[claude] 스캠 위험도 분석 실패 → fallback 분석 사용")
        return _fallback_scam_analysis(description, seller_rating, seller_transaction_count)


# 독소 조항 키워드 → 표준 영어 태그. 입력 텍스트는 다국어(한국어 포함)를 계속
# 인식하지만, 출력 태그는 항상 STANDARD_RISK_TAGS의 영어 표준 용어로만 나간다.
_TOXIC_KEYWORD_TAGS = {
    "junk": "As-Is / Parts Only",
    "부품용": "As-Is / Parts Only",
    "parts only": "As-Is / Parts Only",
    "as-is": "As-Is / Parts Only",
    "as is": "As-Is / Parts Only",
    "no return": "No Returns Accepted",
    "반품불가": "No Returns Accepted",
}
_MIN_TRUSTED_SELLER_RATING = 95.0
_MIN_TRUSTED_TRANSACTION_COUNT = 10


def _fallback_scam_analysis(
    description: str,
    seller_rating: Optional[float],
    seller_transaction_count: Optional[int],
) -> ClaudeScamResult:
    """API 미설정/실패 시 사용하는 키워드 + 판매자 신뢰도 기반 규칙 fallback.

    seller_rating/seller_transaction_count가 None(RSS 등에서 알 수 없는 경우)이면
    "New/Unverified Seller"로 취급한다 — 신뢰도를 확인할 수 없다는 사실 자체가
    보수적으로 봐야 할 신호이기 때문이다.
    """
    lower = description.lower()
    matched_tags = []
    for keyword, tag in _TOXIC_KEYWORD_TAGS.items():
        if keyword in lower and tag not in matched_tags:
            matched_tags.append(tag)

    if matched_tags:
        return ClaudeScamResult(scam_risk="High", risk_reasons=matched_tags)

    trust_tags = []
    if seller_rating is None or seller_transaction_count is None:
        trust_tags.append("New/Unverified Seller")
    else:
        if seller_rating < _MIN_TRUSTED_SELLER_RATING:
            trust_tags.append("Low Rating Seller")
        if seller_transaction_count < _MIN_TRUSTED_TRANSACTION_COUNT:
            trust_tags.append("New/Unverified Seller")

    if trust_tags:
        return ClaudeScamResult(scam_risk="Medium", risk_reasons=trust_tags)

    return ClaudeScamResult(scam_risk="Low", risk_reasons=[])
