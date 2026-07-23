"""
이베이(eBay) 공개 RSS 검색 피드 수집기.

일반 검색 결과 HTML 페이지는 Cloudflare/봇 탐지에 쉽게 막히므로, 이베이가
공식 제공하는 RSS 피드(`_rss=1`)를 대신 사용한다. `feedparser`로 파싱해서
`RawCameraItem`으로 매핑한다.

한계 (정직하게 문서화해둔다):
- eBay RSS의 <description>은 판매자가 작성한 본문 전체가 아니라 가격/썸네일
  위주의 짧은 요약이다. 상태/독소조항을 판단할 진짜 본문이나 이미지 갤러리
  전체가 필요하면 eBay Browse API(OAuth 인증) 연동이 필요하다 — 이 모듈은
  그 전 단계의 "초경량" 수집기로, 얻을 수 있는 최소한의 신호(제목, 가격,
  링크, 썸네일 1장)만 사용한다.
- seller_rating / seller_transaction_count는 RSS에 없으므로 항상 None으로
  채운다 (ai_claude.py는 None을 "판매자 신뢰도 미검증" 신호로 취급한다).
- 이베이가 RSS 엔드포인트를 차단하거나(403 등) 응답 형식을 바꾸면 예외를
  삼키고 빈 리스트를 반환한다 — 한 키워드의 실패로 전체 수집 사이클이
  죽으면 안 되기 때문이다.
"""
import asyncio
import re
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.logger import get_logger
from app.schemas.camera import CameraCurrency, CameraPlatform, RawCameraItem

logger = get_logger(__name__)

_RSS_BASE_URL = "https://www.ebay.com/sch/i.html"

_ITEM_ID_PATTERN = re.compile(r"/itm/(?:[^/?]+/)?(\d+)")
_PRICE_PATTERN = re.compile(r"(?:US\s*)?\$\s*([\d,]+\.\d{2})")


def build_search_rss_url(keyword: str) -> str:
    """검색 키워드로 이베이 RSS 검색 URL을 만든다 (쿼리 인코딩은 httpx가 처리)."""
    url = httpx.URL(_RSS_BASE_URL, params={"_nkw": keyword, "_rss": "1"})
    return str(url)


async def _fetch_raw_rss(keyword: str) -> str:
    """RSS XML을 텍스트로 가져온다. 실패하면 예외를 그대로 올린다(호출부가 처리)."""
    url = build_search_rss_url(keyword)
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


def _extract_item_id(link: str) -> str:
    """링크에서 이베이 상품 번호를 뽑아 item_id로 쓴다. 못 찾으면 링크 해시로 대체."""
    match = _ITEM_ID_PATTERN.search(link)
    if match:
        return f"ebay-{match.group(1)}"
    return f"ebay-{abs(hash(link))}"


def _extract_price(text: str) -> Optional[float]:
    match = _PRICE_PATTERN.search(text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _extract_image_url(entry) -> Optional[str]:
    """<media:thumbnail>을 우선 쓰고, 없으면 description HTML 안의 첫 <img>를 찾는다."""
    thumbnails = getattr(entry, "media_thumbnail", None)
    if thumbnails:
        url = thumbnails[0].get("url")
        if url:
            return url

    html = getattr(entry, "summary", "") or ""
    soup = BeautifulSoup(html, "lxml")
    img = soup.find("img")
    if img and img.get("src"):
        return img["src"]
    return None


def _parse_entry(entry) -> Optional[RawCameraItem]:
    """feedparser entry 하나를 RawCameraItem으로 변환한다. 필수 정보가 없으면 None."""
    title = getattr(entry, "title", "").strip()
    link = getattr(entry, "link", "").strip()
    if not title or not link:
        return None

    html_description = getattr(entry, "summary", "") or ""
    plain_description = BeautifulSoup(html_description, "lxml").get_text(separator=" ", strip=True)

    price = _extract_price(plain_description)
    if price is None:
        price = _extract_price(title)
    if price is None:
        logger.warning("[ebay_rss] 가격을 찾을 수 없어 건너뜀: %s", title)
        return None

    image_url = _extract_image_url(entry)

    try:
        return RawCameraItem(
            item_id=_extract_item_id(link),
            name=title,
            source_url=link,
            currency=CameraCurrency.USD,
            platform=CameraPlatform.EBAY,
            price=price,
            seller_rating=None,
            seller_transaction_count=None,
            description=f"{title}. {plain_description}".strip(),
            image_urls=[image_url] if image_url else [],
        )
    except Exception:
        logger.exception("[ebay_rss] RawCameraItem 변환 실패, 건너뜀: %s", title)
        return None


async def fetch_ebay_rss_items(keyword: str) -> list:
    """키워드로 이베이 RSS를 수집해서 RawCameraItem 리스트로 변환한다.

    네트워크/파싱 실패 시 예외를 삼키고 빈 리스트를 반환한다 — 호출부(스케줄러)가
    한 키워드의 실패 때문에 전체 수집 사이클을 멈추지 않도록 하기 위함이다.
    """
    try:
        raw_xml = await _fetch_raw_rss(keyword)
    except Exception:
        logger.exception("[ebay_rss] '%s' RSS 요청 실패", keyword)
        return []

    # feedparser.parse는 동기(CPU-bound) 함수라 이벤트 루프를 막지 않도록 스레드로 뺀다.
    feed = await asyncio.to_thread(feedparser.parse, raw_xml)
    if feed.bozo and not feed.entries:
        logger.warning(
            "[ebay_rss] '%s' 피드 파싱 실패: %s", keyword, getattr(feed, "bozo_exception", "unknown")
        )
        return []

    items = [item for entry in feed.entries if (item := _parse_entry(entry)) is not None]

    logger.info("[ebay_rss] '%s' → %d건 파싱 성공 (전체 entries=%d)", keyword, len(items), len(feed.entries))
    return items
