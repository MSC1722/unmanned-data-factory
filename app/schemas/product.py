"""
수집된 매물 데이터를 정형화하는 Pydantic 스키마.

어떤 스크레이퍼(이베이/야후옥션/RSS ...)를 쓰든, 이 모델로 변환된 이후부터는
파이프라인의 나머지 부분(로깅, 적재, 추후 AI 분석)이 소스에 상관없이 동일하게 동작한다.
"""
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class ProductItem(BaseModel):
    """스크레이핑한 매물 1건을 나타내는 정형 데이터."""

    source: str = Field(..., description="수집 출처. 예: 'ebay', 'yahoo_auction'")
    source_url: HttpUrl = Field(..., description="원본 상품 페이지 URL")

    name: str = Field(..., min_length=1, description="상품명")
    price: float = Field(..., ge=0, description="가격 (숫자만, 통화 기호 제외)")
    currency: str = Field(default="USD", description="통화 코드. 예: USD, JPY, KRW")

    image_urls: list[HttpUrl] = Field(default_factory=list, description="상품 이미지 URL 배열")
    description: str = Field(default="", description="상품 본문 텍스트 (설명)")

    scraped_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="스크레이핑이 수행된 시각 (UTC)",
    )

    # 향후 AI 분석 단계(번역/카테고리 분류/가격 적정성 판단 등)에서
    # 원본 힌트가 필요할 수 있어 확장 여지를 남겨둔다.
    extra: Optional[dict] = Field(default=None, description="소스별 부가 정보 (자유 형식)")

    @field_validator("name", "description")
    @classmethod
    def _strip_whitespace(cls, value: str) -> str:
        return value.strip()


class ScrapeJobResult(BaseModel):
    """스크레이핑 작업 1회 실행 결과 요약. API 응답 및 로깅에 사용."""

    source: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    item_count: int
    items: list[ProductItem]
