"""
'하이엔드 빈티지/프리미엄 카메라 및 렌즈' 도메인 전용 Pydantic 스키마.

수집(RawCameraItem) → AI 분석/연산 후 송출(ProcessedCameraItem)로 이어지는
2단계 구조를 스키마 레벨에서 명확히 분리한다.
- RawCameraItem : 스크레이퍼가 그대로 긁어온 원본 데이터.
- ProcessedCameraItem : 원본(raw) + AI 분석 결과 + USD 기준 원가/마진 계산
  + 국가별 구매 링크 + 시그널 트리거 여부.
  raw 필드에 RawCameraItem 전체를 그대로 품고 있어 원본 데이터를 잃지 않는다.

전 세계 리셀러 대상 대시보드이므로 모든 금액 연산은 미국 달러(USD) 기준으로
통일한다. 원본 통화(JPY 등)는 raw.price/raw.currency에 그대로 남기고,
pricing 블록에서 USD 환산 과정을 투명하게 보여준다.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class CameraCurrency(str, Enum):
    """해외 매물 판매가의 원본 통화 코드."""

    USD = "USD"
    JPY = "JPY"


class CameraPlatform(str, Enum):
    """매물 수집 출처 플랫폼."""

    EBAY = "eBay"
    YAHOO_AUCTION = "Yahoo_Auction"
    MERCARI = "Mercari"


class ConditionGrade(str, Enum):
    """AI가 판정하는 외관 상태 등급."""

    MINT = "Mint"
    EXCELLENT = "Excellent"
    VERY_GOOD = "Very Good"
    FAIR = "Fair"


class ScamRiskLevel(str, Enum):
    """스캠/독소조항(부품용, 노리턴, 미작동 등) 위험도."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class RawCameraItem(BaseModel):
    """스크레이퍼가 대상 사이트에서 그대로 긁어온 원본 매물 데이터."""

    item_id: str = Field(..., description="플랫폼 내 상품 ID")
    name: str = Field(..., min_length=1, description="상품명")
    source_url: HttpUrl = Field(..., description="원본 상품 페이지 URL")

    currency: CameraCurrency = Field(..., description="해외 판매가의 원본 통화 코드 (USD 또는 JPY)")
    platform: CameraPlatform = Field(..., description="수집 출처 플랫폼 (eBay, Yahoo_Auction, Mercari)")
    price: float = Field(..., ge=0, description="해외 판매가 (원본 통화 기준, 통화 기호 제외)")

    # RSS 기반 수집기(app/scrapers/ebay_rss.py)는 판매자 신뢰도 정보를 제공하지
    # 않아 None일 수 있다. eBay Browse API 등 인증된 소스로 전환하면 채워진다.
    seller_rating: Optional[float] = Field(
        default=None, ge=0, le=100, description="판매자 평점 (0~100, % 긍정 피드백 기준). 알 수 없으면 None"
    )
    seller_transaction_count: Optional[int] = Field(
        default=None, ge=0, description="판매자 누적 거래 건수. 알 수 없으면 None"
    )

    description: str = Field(default="", description="상품 본문 텍스트")
    image_urls: list[HttpUrl] = Field(default_factory=list, description="상품 이미지 URL 리스트")

    scraped_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="스크레이핑이 수행된 시각 (UTC)",
    )

    @field_validator("name", "description")
    @classmethod
    def _strip_whitespace(cls, value: str) -> str:
        return value.strip()


class AIAnalysis(BaseModel):
    """AI(Gemini 비전 + Claude 텍스트, 실패 시 규칙 기반 fallback)가 산출하는 분석 결과."""

    condition_grade: ConditionGrade = Field(..., description="외관 상태 등급")
    condition_score: int = Field(..., ge=0, le=100, description="Gemini가 산정한 외관 상태 점수 (0~100)")
    detected_defects: list[str] = Field(default_factory=list, description="감지된 결함 리스트")
    scam_risk: ScamRiskLevel = Field(..., description="스캠/독소조항 위험도")
    scam_risk_reasons: list[str] = Field(
        default_factory=list,
        description="Claude가 산출한 위험도 판정 근거 (STANDARD_RISK_TAGS의 영어 표준 태그만 포함)",
    )


class PricingBreakdown(BaseModel):
    """USD 기준으로 계산하는 원가/마진 분석 결과. (AI 아님, 순수 연산)

    원본 통화/가격부터 최종 USD 환산액까지 그대로 노출해서, 전 세계 어느
    사용자가 봐도 환산 과정을 투명하게 검증할 수 있게 한다.
    """

    original_price: float = Field(..., description="원본 판매가 (원본 통화 기준)")
    original_currency: CameraCurrency = Field(..., description="원본 통화 코드")
    exchange_rate_to_usd: float = Field(..., description="원본 통화 1단위당 USD 환율 (USD면 1.0)")

    foreign_buy_price_usd: float = Field(..., description="해외 구매가 (USD 환산)")
    global_shipping_fee_usd: float = Field(..., description="글로벌 표준 배송비 (USD, 고정 상수)")
    final_import_cost_usd: float = Field(
        ..., description="최종 수입 원가 (USD) = 해외구매가 + 배송비"
    )
    global_baseline_price_usd: float = Field(..., description="글로벌 기준가 (USD, 목표 판매가)")
    net_profit_usd: float = Field(
        ..., description="예상 순수익 (USD) = 글로벌 기준가 - 최종 수입 원가"
    )
    margin_rate_percent: float = Field(..., description="마진율(%) = 순수익 / 글로벌 기준가 * 100")


class PurchaseLink(BaseModel):
    """리셀러가 실제로 매물을 구매할 수 있는 링크. 국가/지역별로 여러 개일 수 있다."""

    country_code: str = Field(
        ..., description="이 링크로 구매 가능한 지역 코드. 전 세계 공통이면 'GLOBAL'"
    )
    label: str = Field(..., description="링크 설명 (예: 'eBay 직접구매', 'Buyee 일본 대리구매')")
    url: HttpUrl = Field(..., description="구매 페이지 URL")


class ProcessedCameraItem(BaseModel):
    """AI 분석 및 USD 원가/마진 연산을 마치고 대시보드로 송출되는 매물 데이터."""

    raw: RawCameraItem = Field(..., description="원본 수집 데이터 전체")
    ai_analysis: AIAnalysis = Field(..., description="AI 분석 결과")
    pricing: PricingBreakdown = Field(..., description="USD 기준 원가/마진 계산 결과")
    purchase_links: list[PurchaseLink] = Field(
        default_factory=list, description="국가/지역별 구매 가능 링크"
    )

    discovered_at: datetime = Field(
        ..., description="이 매물이 시스템에 처음 포착된 시각 (UTC). free-signals의 30분 지연 필터 기준."
    )

    is_trigger: bool = Field(..., description="구매 시그널 발송 여부")
    is_premium: bool = Field(
        ...,
        description="프리미엄(유료) 등급 여부. margin_rate_percent>=20 또는 condition_score>=90일 때 True",
    )

    processed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="AI 분석/연산이 완료된 시각 (UTC)",
    )


class CameraTestFeedResponse(BaseModel):
    """/api/v1/test-feed 응답 래퍼."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    item_count: int
    items: list[ProcessedCameraItem]


class SignalsResponse(BaseModel):
    """/api/v1/signals 응답 래퍼. 대시보드가 주기적으로 폴링하는 실시간 시그널 피드."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signal_count: int
    signals: list[ProcessedCameraItem] = Field(
        ..., description="is_trigger=True 매물만, margin_rate_percent 내림차순 정렬"
    )
