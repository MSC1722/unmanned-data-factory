"""
이베이(eBay) 스타일 매물 스크레이퍼.

실제 타깃 사이트의 CSS 선택자가 정해지기 전까지는 `USE_MOCK_DATA=true`로 두고
내장 목(mock) 데이터로 파이프라인 전체(수집→검증→로깅→적재)를 검증한다.
selector만 실제 페이지 구조에 맞게 교체하면 바로 실사용으로 전환할 수 있다.
"""
import re

from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.core.config import settings
from app.core.logger import get_logger
from app.schemas.product import ProductItem
from app.scrapers.base import BaseScraper

logger = get_logger(__name__)

# "$1,234.56" / "US $99.00" 같은 문자열에서 숫자만 뽑아내기 위한 패턴
_PRICE_PATTERN = re.compile(r"[\d,]+\.?\d*")


def _parse_price(price_text: str) -> float:
    """가격 문자열에서 통화 기호/콤마를 제거하고 float으로 변환한다."""
    match = _PRICE_PATTERN.search(price_text.replace(",", ""))
    if not match:
        raise ValueError(f"가격을 파싱할 수 없음: {price_text!r}")
    return float(match.group())


class EbayScraper(BaseScraper):
    """이베이(또는 동일한 리스트 구조를 가진 사이트)용 스크레이퍼."""

    source = "ebay"

    def parse(self, raw: str) -> list[ProductItem]:
        """상품 리스트 HTML을 파싱해서 ProductItem 리스트로 변환한다.

        NOTE: 아래 CSS 선택자(.product-card 등)는 예시용 placeholder다.
        실제 타깃 사이트의 DOM 구조가 정해지면 이 부분만 교체하면 된다.
        """
        soup = BeautifulSoup(raw, "lxml")
        items: list[ProductItem] = []

        for card in soup.select(".product-card"):
            try:
                name_el = card.select_one(".product-name")
                price_el = card.select_one(".product-price")
                link_el = card.select_one("a[href]")
                if not (name_el and price_el and link_el):
                    continue

                desc_el = card.select_one(".product-desc")
                image_urls = [img["src"] for img in card.select("img[src]")]

                item = ProductItem(
                    source=self.source,
                    source_url=link_el["href"],
                    name=name_el.get_text(strip=True),
                    price=_parse_price(price_el.get_text(strip=True)),
                    image_urls=image_urls,
                    description=desc_el.get_text(strip=True) if desc_el else "",
                )
                items.append(item)
            except (ValueError, ValidationError) as exc:
                # 카드 하나가 깨져도 전체 배치를 실패시키지 않고 건너뛴다.
                logger.warning("[%s] 상품 카드 파싱 실패, 건너뜀: %s", self.source, exc)
                continue

        return items

    async def run(self) -> list[ProductItem]:
        """목 데이터 모드면 네트워크 요청 없이 바로 목 데이터를 반환한다."""
        if settings.use_mock_data:
            logger.info("[%s] USE_MOCK_DATA=true → 목 데이터로 대체", self.source)
            items = self._mock_items()
            logger.info("[%s] 스크레이핑 완료(mock): %d건 수집", self.source, len(items))
            return items
        return await super().run()

    def _mock_items(self) -> list[ProductItem]:
        """실제 타깃이 정해지기 전, 파이프라인 검증용 내장 목 데이터."""
        return [
            ProductItem(
                source=self.source,
                source_url="https://www.ebay.com/itm/000000000001",
                name="Vintage Seiko 5 Automatic Watch",
                price=89.99,
                currency="USD",
                image_urls=[
                    "https://example.com/images/seiko5_1.jpg",
                    "https://example.com/images/seiko5_2.jpg",
                ],
                description="1970년대 생산된 세이코5 오토매틱 시계. 작동 양호, 약간의 사용感 있음.",
            ),
            ProductItem(
                source=self.source,
                source_url="https://www.ebay.com/itm/000000000002",
                name="Nintendo Game Boy Color Console",
                price=45.50,
                currency="USD",
                image_urls=["https://example.com/images/gbc_1.jpg"],
                description="정상 작동하는 게임보이 컬러 본체. 배터리 커버 포함, 게임 카트리지 별도.",
            ),
        ]
