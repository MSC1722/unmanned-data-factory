"""
카메라/렌즈 파이프라인 로직 테스트용 Mock 데이터.

실제 스크레이퍼 대신 RawCameraItem을 직접 만들어 process_raw_camera_item에
흘려보냄으로써, "AI 분석(Gemini+Claude, 캐싱 적용) → USD 원가 연산 →
발송/프리미엄 판정 → 30분 지연" 체인 전체가 의도대로 동작하는지 검증한다.
GEMINI_API_KEY/CLAUDE_API_KEY가 없는 개발 환경에서도 각 서비스의 규칙 기반
fallback이 동작하므로 그대로 네트워크 없이 실행된다.

각 매물의 raw.scraped_at(=ProcessedCameraItem.discovered_at)을 의도적으로
다르게 세팅해서, free-signals의 "30분 지연" 필터가 실제로 무언가를
걸러내고 있다는 걸 보여준다:

케이스 1: 라이카 M6 (eBay, USD, 5분 전 발견) — 마진 25% → 트리거 + 프리미엄
          (마진 경로). 프리미엄은 지연이 없으므로 발견 5분 만에도 즉시 노출.
케이스 2: Junk 후지필름 X100V (Yahoo Auction, JPY, 20분 전 발견) — 마진은
          표면상 높지만 스캠 필터에 걸려 트리거 자체가 되지 않는다
          (free/premium/signals 어디에도 안 뜸).
케이스 3: 니콘 FM2 (eBay, USD, 45분 전 발견) — 마진 ~15% → 트리거는 되지만
          프리미엄 미달. 발견된 지 30분이 지나 free-signals에 노출된다.
케이스 4: 콘탁스 T2 (Mercari, JPY, 2분 전 발견) — 마진 35% → 프리미엄(마진
          경로). 방금 발견됐어도 프리미엄은 지연 없이 바로 노출된다.
케이스 5: 올림푸스 OM-1 MD (eBay, USD, 5분 전 발견) — 마진 ~12% → 트리거는
          되지만 프리미엄 미달. 발견된 지 30분이 안 지나 free-signals에서는
          "아직" 보이지 않는다 (전체 피드인 /signals에는 보인다) — 30분 지연
          필터가 실제로 뭔가를 걸러내고 있음을 보여주는 핵심 케이스.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.schemas.camera import CameraCurrency, CameraPlatform, ProcessedCameraItem, RawCameraItem
from app.services.camera_pipeline import process_raw_camera_item


def _minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


async def build_good_leica_m6() -> ProcessedCameraItem:
    """정상 양품 라이카 M6 매물 (USD, eBay). 글로벌 기준가는 마진율이 정확히
    25%가 되도록 (최종 수입 원가 / 0.75) 역산해뒀다."""
    raw = RawCameraItem(
        item_id="ebay-000111222",
        name="Leica M6 Classic 0.72 Chrome Body",
        source_url="https://www.ebay.com/itm/000111222",
        currency=CameraCurrency.USD,
        platform=CameraPlatform.EBAY,
        price=1300.0,
        seller_rating=99.8,
        seller_transaction_count=1523,
        description=(
            "Leica M6 Classic 0.72 viewfinder, chrome body. Clean glass, "
            "accurate shutter speeds, meter works perfectly. Genuine light "
            "brassing on top plate only from normal use. Comes with original "
            "strap, caps, and box."
        ),
        image_urls=[
            "https://example.com/images/leica_m6_1.jpg",
            "https://example.com/images/leica_m6_2.jpg",
        ],
        scraped_at=_minutes_ago(5),
    )
    # 최종 수입 원가 = $1300(USD 그대로) + $40(배송비) = $1340.
    # 마진 25%를 만들려면 글로벌 기준가 = 수입원가 / (1 - 0.25) = 수입원가 / 0.75
    global_baseline_price_usd = 1786.67
    return await process_raw_camera_item(raw, global_baseline_price_usd=global_baseline_price_usd)


async def build_junk_x100v_scam() -> ProcessedCameraItem:
    """Junk 후지필름 X100V 매물 (JPY, Yahoo Auction). 가격이 싸서 표면
    마진율은 오히려 더 높게 나오지만, 스캠 필터(본문의 'Junk', 'As-is',
    'no returns')에 걸려 시그널이 억제돼야 한다."""
    raw = RawCameraItem(
        item_id="yja-99887766",
        name="Fujifilm X100V Black",
        source_url="https://page.auctions.yahoo.co.jp/jp/auction/99887766",
        currency=CameraCurrency.JPY,
        platform=CameraPlatform.YAHOO_AUCTION,
        price=45_000.0,
        seller_rating=97.5,
        seller_transaction_count=412,
        description=(
            "Fujifilm X100V - Junk, for parts only. "
            "Shutter not working, LCD cracked. As-is, no returns accepted."
        ),
        image_urls=["https://example.com/images/x100v_junk_1.jpg"],
        scraped_at=_minutes_ago(20),
    )
    # 정상 개체 기준 글로벌 시세를 기준가로 둔다 (이 매물 자체는 정상이 아님을
    # 스캠 필터가 잡아내는지 확인하려는 의도).
    global_baseline_price_usd = 1000.0
    return await process_raw_camera_item(raw, global_baseline_price_usd=global_baseline_price_usd)


async def build_free_tier_nikon_fm2() -> ProcessedCameraItem:
    """니콘 FM2 매물 (USD, eBay). 트리거는 되지만 마진<20%, 점수<90이라
    프리미엄 미달 → 발견 45분 경과로 30분 지연도 풀려 free-signals에 노출."""
    raw = RawCameraItem(
        item_id="ebay-000333444",
        name="Nikon FM2 Black Chrome Body",
        source_url="https://www.ebay.com/itm/000333444",
        currency=CameraCurrency.USD,
        platform=CameraPlatform.EBAY,
        price=220.0,
        seller_rating=98.5,
        seller_transaction_count=640,
        description=(
            "Nikon FM2 black chrome body, fully mechanical. Meter is accurate, "
            "all shutter speeds fire correctly. Light brassing on edges from "
            "normal use. Comes with body cap."
        ),
        image_urls=["https://example.com/images/nikon_fm2_1.jpg"],
        scraped_at=_minutes_ago(45),
    )
    # 수입 원가 = $220 + $40 = $260. 기준가 $305 → 마진 약 14.75% (트리거 O, 프리미엄 X)
    global_baseline_price_usd = 305.0
    return await process_raw_camera_item(raw, global_baseline_price_usd=global_baseline_price_usd)


async def build_premium_tier_contax_t2() -> ProcessedCameraItem:
    """콘탁스 T2 매물 (JPY, Mercari). 마진율이 프리미엄 임계값(20%)을 넘어
    premium-signals에 노출되는 예시 — eBay 외 플랫폼/통화 조합도 보여준다.
    방금 발견됐어도(2분 전) 프리미엄은 지연 없이 바로 노출된다."""
    raw = RawCameraItem(
        item_id="mercari-m998877",
        name="Contax T2 Titan Black",
        source_url="https://jp.mercari.com/item/m998877",
        currency=CameraCurrency.JPY,
        platform=CameraPlatform.MERCARI,
        price=90_000.0,
        seller_rating=96.0,
        seller_transaction_count=210,
        description=(
            "Contax T2 Titan Black point-and-shoot. Lens is clean with no "
            "fungus or haze. Winder and flash both tested working. Light wear "
            "on the titanium body consistent with age."
        ),
        image_urls=["https://example.com/images/contax_t2_1.jpg"],
        scraped_at=_minutes_ago(2),
    )
    # 환산가 = 90,000엔 / 150 = $600. 수입 원가 = $600 + $40 = $640.
    # 기준가 = 수입원가 / (1 - 0.35) ≈ $984.62 → 마진 약 35% (프리미엄, 마진 경로)
    global_baseline_price_usd = 984.62
    return await process_raw_camera_item(raw, global_baseline_price_usd=global_baseline_price_usd)


async def build_embargoed_free_tier_olympus_om1() -> ProcessedCameraItem:
    """올림푸스 OM-1 MD 매물 (USD, eBay). free-signals 자격(트리거 O, 프리미엄 X)은
    되지만 발견된 지 5분밖에 안 지나 30분 지연에 걸려 free-signals에는 아직 안 뜬다.
    (전체 피드인 /signals에는 뜬다 — "지연이 실제로 걸러내고 있다"를 보여주는 케이스)"""
    raw = RawCameraItem(
        item_id="ebay-000555666",
        name="Olympus OM-1 MD Black",
        source_url="https://www.ebay.com/itm/000555666",
        currency=CameraCurrency.USD,
        platform=CameraPlatform.EBAY,
        price=150.0,
        seller_rating=97.0,
        seller_transaction_count=88,
        description=(
            "Olympus OM-1 MD black body. Meter works with 1.5V battery "
            "(needs adapter). Shutter curtain clean, no light leaks. "
            "Normal cosmetic wear for its age."
        ),
        image_urls=["https://example.com/images/om1_md_1.jpg"],
        scraped_at=_minutes_ago(5),
    )
    # 수입 원가 = $150 + $40 = $190. 기준가 $215.91 → 마진 약 12% (트리거 O, 프리미엄 X)
    global_baseline_price_usd = 215.91
    return await process_raw_camera_item(raw, global_baseline_price_usd=global_baseline_price_usd)


async def get_mock_test_feed() -> list:
    """/api/v1/test-feed 등에서 쓰는 Mock 매물 5건을 병렬로 생성한다."""
    return list(
        await asyncio.gather(
            build_good_leica_m6(),
            build_junk_x100v_scam(),
            build_free_tier_nikon_fm2(),
            build_premium_tier_contax_t2(),
            build_embargoed_free_tier_olympus_om1(),
        )
    )
