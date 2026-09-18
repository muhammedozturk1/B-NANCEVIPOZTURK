"""
Destek/Direnç Teyidi Stratejisi.

Klasik pivot formülüyle (önceki periyodun H/L/C'sinden) S/R seviyeleri
hesaplanır. Fiyat bu seviyelere yakınsa ve likidite bölgeleriyle çakışıyorsa
"teyit edilmiş" güçlü bir S/R bölgesi olarak değerlendirilir.
"""
from backend import config
from backend.strategies.liquidity import find_liquidity_zones


def calculate_pivot_points(ohlcv: list) -> dict:
    """Bir önceki tam periyodun H/L/C'sinden klasik pivot noktalarını hesaplar."""
    prev_candle = ohlcv[-2]
    high, low, close = prev_candle[2], prev_candle[3], prev_candle[4]

    pivot = (high + low + close) / 3
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)

    return {"pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2}


def get_signal(ohlcv: list) -> dict:
    if len(ohlcv) < 3:
        return {"direction": "neutral", "reason": "yetersiz veri"}

    levels = calculate_pivot_points(ohlcv)
    current_price = ohlcv[-1][4]
    threshold = config.SR_PROXIMITY_THRESHOLD

    resistance_liquidity, support_liquidity = find_liquidity_zones(ohlcv)
    liquidity_prices = [z["price"] for z in resistance_liquidity + support_liquidity]

    def is_confirmed_by_liquidity(level_price):
        return any(abs(level_price - lp) / lp <= threshold for lp in liquidity_prices)

    direction = "neutral"
    matched_level = None
    confirmed = False

    for name, price in [("s1", levels["s1"]), ("s2", levels["s2"])]:
        if abs(current_price - price) / price <= threshold:
            direction = "long"
            matched_level = name
            confirmed = is_confirmed_by_liquidity(price)
            break

    if direction == "neutral":
        for name, price in [("r1", levels["r1"]), ("r2", levels["r2"])]:
            if abs(current_price - price) / price <= threshold:
                direction = "short"
                matched_level = name
                confirmed = is_confirmed_by_liquidity(price)
                break

    return {
        "direction": direction,
        "matched_level": matched_level,
        "liquidity_confirmed": confirmed,   # True ise: destek/direnç + likidite çakışıyor -> daha güçlü sinyal
        "levels": levels,
    }
