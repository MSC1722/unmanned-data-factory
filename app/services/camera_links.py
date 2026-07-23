"""
국가/지역별 구매 가능 링크 생성 모듈.

eBay는 전 세계 어디서든 직접 구매/배송이 가능하지만, 일본 야후옥션·메르카리는
일본 거주자가 아니면 직접 구매가 불가능해 대리구매(프록시 쇼핑) 서비스를
거쳐야 한다. 그래서 플랫폼별로 실제 리셀러가 결제까지 갈 수 있는 링크를
다르게 구성해서 반환한다.

NOTE: 야후옥션/메르카리의 대리구매 URL 패턴(Buyee, ZenMarket 등)은 실제
제휴(어필리에이트) 연동 전까지 쓰는 예시 템플릿이다. 실제 서비스 적용 시
각 대리구매 업체의 공식 딥링크/제휴 파라미터로 교체해야 한다.
"""
from app.schemas.camera import CameraPlatform, PurchaseLink, RawCameraItem


def build_purchase_links(raw: RawCameraItem) -> list:
    """플랫폼에 맞는 국가/지역별 구매 링크 목록을 만든다."""
    if raw.platform == CameraPlatform.EBAY:
        return [
            PurchaseLink(
                country_code="GLOBAL",
                label="eBay (Direct Purchase, Worldwide Shipping)",
                url=raw.source_url,
            )
        ]

    if raw.platform == CameraPlatform.YAHOO_AUCTION:
        return [
            PurchaseLink(
                country_code="GLOBAL",
                label="Buyee (Japan Proxy Shopping)",
                url=f"https://buyee.jp/item/yahoo/auction/{raw.item_id}",
            ),
            PurchaseLink(
                country_code="GLOBAL",
                label="ZenMarket (Japan Proxy Shopping)",
                url=f"https://zenmarket.jp/en/auction.aspx?itemCode={raw.item_id}",
            ),
            PurchaseLink(
                country_code="JP",
                label="Yahoo Auction Original Listing (Japan Residents Only)",
                url=raw.source_url,
            ),
        ]

    if raw.platform == CameraPlatform.MERCARI:
        return [
            PurchaseLink(
                country_code="GLOBAL",
                label="Buyee (Japan Proxy Shopping)",
                url=f"https://buyee.jp/item/mercari/item/{raw.item_id}",
            ),
            PurchaseLink(
                country_code="JP",
                label="Mercari Original Listing (Japan Residents Only)",
                url=raw.source_url,
            ),
        ]

    raise ValueError(f"지원하지 않는 플랫폼: {raw.platform}")
