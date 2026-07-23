"""
스크레이퍼 공통 인터페이스.

모든 소스별 스크레이퍼(이베이, 야후옥션, RSS ...)는 이 BaseScraper를 상속해서
`fetch_raw()`(네트워크 IO)와 `parse()`(순수 파싱 로직)만 구현하면 된다.
IO와 파싱을 분리해두면, parse()는 네트워크 없이 단위 테스트할 수 있다.
"""
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logger import get_logger
from app.schemas.product import ProductItem

logger = get_logger(__name__)


class BaseScraper(ABC):
    """모든 소스별 스크레이퍼가 상속받는 추상 클래스."""

    #: 하위 클래스에서 반드시 지정. 로그/스키마의 `source` 필드로 쓰인다.
    source: str = "unknown"

    def __init__(self, target_url: Optional[str] = None) -> None:
        # 소스마다 base URL이 다를 수 있어 생성자에서 override 가능하게 둔다.
        self.target_url = target_url or settings.target_base_url

    async def fetch_raw(self) -> str:
        """대상 URL에서 원본 텍스트(HTML/RSS 등)를 비동기로 가져온다."""
        headers = {"User-Agent": settings.user_agent}
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            response = await client.get(self.target_url, headers=headers)
            response.raise_for_status()
            return response.text

    @abstractmethod
    def parse(self, raw: str) -> list[ProductItem]:
        """원본 텍스트를 파싱해서 정형화된 ProductItem 리스트로 변환한다.

        네트워크 IO가 없는 순수 함수로 유지해서 테스트하기 쉽게 만든다.
        """
        raise NotImplementedError

    async def run(self) -> list[ProductItem]:
        """fetch → parse를 순서대로 실행하는 진입점."""
        logger.info("[%s] 스크레이핑 시작: %s", self.source, self.target_url)
        raw = await self.fetch_raw()
        items = self.parse(raw)
        logger.info("[%s] 스크레이핑 완료: %d건 수집", self.source, len(items))
        return items
