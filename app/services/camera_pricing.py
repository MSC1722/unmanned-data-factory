"""
카메라/렌즈 매물의 USD 기준 원가·마진 계산 모듈. (AI 아님 — 순수 하드코딩 공식)

글로벌 리셀러 대상 SaaS이므로 국가별 관세/부가세 로직은 넣지 않는다
(국가마다 규정이 달라 리셀러 각자가 알아서 처리하는 영역으로 본다).
공식은 다음과 같이 단순하게 유지한다:

    Final_Import_Cost (USD) = Foreign_Buy_Price (USD) + Global_Shipping_Fee (USD)
    Net_Profit (USD)        = Global_Baseline_Price (USD) - Final_Import_Cost (USD)
    Margin_Rate (%)         = Net_Profit / Global_Baseline_Price * 100
"""
from app.core.config import settings
from app.schemas.camera import CameraCurrency


def get_exchange_rate_to_usd(currency: CameraCurrency) -> float:
    """원본 통화 1단위당 USD 환율을 반환한다. USD는 기준 통화라 1.0 고정."""
    if currency == CameraCurrency.USD:
        return 1.0
    if currency == CameraCurrency.JPY:
        return settings.fx_rate_jpy_usd
    raise ValueError(f"지원하지 않는 통화: {currency}")


def convert_to_usd(price: float, currency: CameraCurrency) -> float:
    """원본 통화 판매가를 USD로 환산한다."""
    return price * get_exchange_rate_to_usd(currency)


def calculate_final_import_cost_usd(foreign_buy_price_usd: float) -> float:
    """최종 수입 원가(USD) = 해외 구매가(USD) + 글로벌 표준 배송비(USD, 고정 $40)."""
    return foreign_buy_price_usd + settings.global_shipping_fee_usd


def calculate_margin_usd(global_baseline_price_usd: float, final_import_cost_usd: float) -> tuple:
    """글로벌 기준가 대비 예상 순수익(USD)과 마진율(%)을 계산한다."""
    net_profit_usd = global_baseline_price_usd - final_import_cost_usd
    margin_rate_percent = (
        (net_profit_usd / global_baseline_price_usd) * 100 if global_baseline_price_usd > 0 else 0.0
    )
    return round(net_profit_usd, 2), round(margin_rate_percent, 2)
