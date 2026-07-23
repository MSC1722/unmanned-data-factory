"""
이베이(eBay) 공식 Browse API 수집기.

RSS 검색 피드(`_rss=1`)가 IP/네트워크 단위로 403 차단되는 것을 로컬에서
직접 확인했다 — Chrome User-Agent로 위장해도 뚫리지 않아, 비공식 HTML/RSS
스크레이핑을 포기하고 OAuth 인증 기반 공식 API로 전환했다.

인증: client_credentials 그랜트(앱 단위 토큰, 사용자 로그인 불필요)로
`https://api.ebay.com/oauth/api_scope` 스코프 토큰을 발급받아
`/buy/browse/v1/item_summary/search`를 호출한다. App ID/Cert ID는
https://developer.ebay.com 에서 애플리케이션을 등록해야 발급된다
(EBAY_CLIENT_ID / EBAY_CLIENT_SECRET 환경변수).

RSS 대비 얻는 것:
- seller.feedbackPercentage / seller.feedbackScore → seller_rating /
  seller_transaction_count를 실제로 채울 수 있다 (RSS는 항상 None이었다).
- 안정적인 200 응답 (공식/인증된 채널이라 봇 차단 대상이 아니다).
- 토큰 만료(기본 2시간) 전까지는 재발급 없이 재사용해 호출 비용을 아낀다.

한계:
- Cert ID(클라이언트 시크릿)가 없으면 토큰 발급 자체가 불가능하다 — 이
  경우 매 사이클 경고 로그만 남기고 빈 리스트를 반환한다(RSS 모듈과 동일한
  "한 소스 실패가 전체를 죽이지 않는다" 원칙).
- Sandbox가 아닌 Production 엔드포인트를 기본으로 쓴다. Rate limit(기본
  5,000 calls/day)을 넘기면 429가 날 수 있다 — 이 모듈도 RSS 모듈처럼
  상태 코드/본문 일부를 에러 로그로 남겨 조기에 알아챌 수 있게 한다.
"""
import time
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logger import get_logger
from app.schemas.camera import CameraCurrency, CameraPlatform, RawCameraItem

logger = get_logger(__name__)

_OAUTH_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

# 프로세스 생애주기 동안 재사용하는 앱 토큰 캐시. (토큰, 만료 unix timestamp)
_cached_token: Optional[tuple[str, float]] = None


async def _get_access_token(client: httpx.AsyncClient) -> Optional[str]:
    """client_credentials 그랜트로 앱 토큰을 발급받는다. 캐시가 유효하면 재사용한다.

    자격증명이 비어 있거나 발급에 실패하면 None을 반환한다(예외를 올리지 않는다) —
    호출부가 "이번 사이클은 건너뛴다"로 처리할 수 있게 하기 위함이다.
    """
    global _cached_token

    if not settings.ebay_client_id or not settings.ebay_client_secret:
        logger.warning("[ebay_browse_api] EBAY_CLIENT_ID/EBAY_CLIENT_SECRET이 비어 있어 토큰 발급을 건너뜀")
        return None

    if _cached_token is not None:
        token, expires_at = _cached_token
        # 만료 60초 전에는 미리 갱신해서 요청 도중 만료되는 경우를 피한다.
        if time.monotonic() < expires_at - 60:
            return token

    try:
        response = await client.post(
            _OAUTH_TOKEN_URL,
            auth=(settings.ebay_client_id, settings.ebay_client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": _OAUTH_SCOPE},
        )
    except httpx.HTTPError:
        logger.exception("[ebay_browse_api] OAuth 토큰 요청 중 네트워크 오류")
        return None

    if response.status_code != 200:
        logger.error(
            "[ebay_browse_api] OAuth 토큰 발급 실패: status=%d body_snippet=%r",
            response.status_code,
            response.text[:500],
        )
        return None

    payload = response.json()
    token = payload["access_token"]
    expires_in = payload.get("expires_in", 7200)
    _cached_token = (token, time.monotonic() + expires_in)
    return token


def _extract_price(item: dict) -> Optional[float]:
    price = item.get("price") or {}
    value = price.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_currency(item: dict) -> Optional[CameraCurrency]:
    code = (item.get("price") or {}).get("currency")
    try:
        return CameraCurrency(code)
    except ValueError:
        return None


def _extract_image_urls(item: dict) -> list[str]:
    urls = []
    image = item.get("image") or {}
    if image.get("imageUrl"):
        urls.append(image["imageUrl"])
    for thumb in item.get("thumbnailImages") or []:
        if thumb.get("imageUrl") and thumb["imageUrl"] not in urls:
            urls.append(thumb["imageUrl"])
    return urls


def _parse_item_summary(item: dict) -> Optional[RawCameraItem]:
    """Browse API의 item_summary 객체 하나를 RawCameraItem으로 변환한다. 필수 정보가 없으면 None."""
    item_id = item.get("itemId")
    title = (item.get("title") or "").strip()
    link = item.get("itemWebUrl")
    if not item_id or not title or not link:
        return None

    price = _extract_price(item)
    currency = _extract_currency(item)
    if price is None or currency is None:
        logger.warning("[ebay_browse_api] 가격/통화를 찾을 수 없어 건너뜀: %s", title)
        return None

    seller = item.get("seller") or {}
    seller_rating = seller.get("feedbackPercentage")
    seller_transaction_count = seller.get("feedbackScore")

    try:
        return RawCameraItem(
            item_id=f"ebay-{item_id}",
            name=title,
            source_url=link,
            currency=currency,
            platform=CameraPlatform.EBAY,
            price=price,
            seller_rating=float(seller_rating) if seller_rating is not None else None,
            seller_transaction_count=int(seller_transaction_count) if seller_transaction_count is not None else None,
            description=item.get("shortDescription") or title,
            image_urls=_extract_image_urls(item),
        )
    except Exception:
        logger.exception("[ebay_browse_api] RawCameraItem 변환 실패, 건너뜀: %s", title)
        return None


async def fetch_ebay_browse_items(keyword: str, limit: int = 50) -> list[RawCameraItem]:
    """키워드로 eBay Browse API를 검색해서 RawCameraItem 리스트로 변환한다.

    네트워크/인증/파싱 실패 시 예외를 삼키고 빈 리스트를 반환한다 — 호출부(스케줄러)가
    한 키워드의 실패 때문에 전체 수집 사이클을 멈추지 않도록 하기 위함이다.
    """
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        token = await _get_access_token(client)
        if token is None:
            return []

        try:
            response = await client.get(
                _SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                },
                params={"q": keyword, "limit": str(limit)},
            )
        except httpx.HTTPError:
            logger.exception("[ebay_browse_api] '%s' 검색 요청 중 네트워크 오류", keyword)
            return []

        if response.status_code != 200:
            logger.error(
                "[ebay_browse_api] '%s' 검색 실패: status=%d body_snippet=%r",
                keyword,
                response.status_code,
                response.text[:500],
            )
            return []

        payload = response.json()

    raw_summaries = payload.get("itemSummaries") or []
    items = [item for summary in raw_summaries if (item := _parse_item_summary(summary)) is not None]

    logger.info(
        "[ebay_browse_api] '%s' → %d건 파싱 성공 (전체 itemSummaries=%d)",
        keyword,
        len(items),
        len(raw_summaries),
    )
    return items
