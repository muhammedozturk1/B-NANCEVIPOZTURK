"""
Likidite Bölgeleri Stratejisi.

Mantık: Piyasada birçok traderın stop-loss/limit emri, eşit (veya çok yakın)
tepe ve dip seviyelerinin hemen ötesinde birikir. Fiyat bu seviyeyi kısaca
"süpürüp" (sweep) geri döndüğünde -> likidite toplanmış demektir ve genellikle
ters yönde güçlü bir hareket başlar. Bu, kendi hesapladığımız (web sitesinden
çekmeden) bir likidite bölgesi modelidir - swing high/low kümelerinden üretilir.
"""
from backend import config
from backend.strategies.smc import find_swing_points


def find_liquidity_zones(ohlcv: list):
    """Birbirine yakın (tolerans içinde) swing high'ları ve swing low'ları
    gruplandırarak likidite bölgelerini (fiyat seviyelerini) döner."""
    swing_highs, swing_lows = find_swing_points(ohlcv)
    tolerance = config.LIQUIDITY_EQUAL_TOLERANCE

    def group_levels(points):
        levels = []
        prices = sorted([p for _, p in points])
        for price in prices:
            placed = False
            for level in levels:
                if abs(price - level["price"]) / level["price"] <= tolerance:
                    level["count"] += 1
                    level["price"] = (level["price"] + price) / 2  # ortalamaya çek
                    placed = True
                    break
            if not placed:
                levels.append({"price": price, "count": 1})
        # Sadece 2+ kez tekrar eden (yani "eşit" kabul edilen) seviyeler gerçek likidite bölgesidir
        return [lvl for lvl in levels if lvl["count"] >= 2]

    resistance_liquidity = group_levels(swing_highs)   # üstte biriken satış-stop likiditesi
    support_liquidity = group_levels(swing_lows)        # altta biriken alış-stop likiditesi

    return resistance_liquidity, support_liquidity


def get_signal(ohlcv: list) -> dict:
    resistance_liquidity, support_liquidity = find_liquidity_zones(ohlcv)

    if not resistance_liquidity and not support_liquidity:
        return {"direction": "neutral", "reason": "belirgin likidite bölgesi yok"}

    current = ohlcv[-1]
    current_high, current_low, current_close = current[2], current[3], current[4]
    prev_close = ohlcv[-2][4]

    direction = "neutral"
    reason = None

    # Üstteki likiditeyi süpürüp geri düştü mü? (sweep + reversal -> short)
    for level in resistance_liquidity:
        if current_high > level["price"] and current_close < level["price"] and prev_close < level["price"]:
            direction = "short"
            reason = f"Üst likidite süpürüldü ({level['price']:.4f}) ve geri düştü"
            break

    # Alttaki likiditeyi süpürüp geri yükseldi mi? (sweep + reversal -> long)
    if direction == "neutral":
        for level in support_liquidity:
            if current_low < level["price"] and current_close > level["price"] and prev_close > level["price"]:
                direction = "long"
                reason = f"Alt likidite süpürüldü ({level['price']:.4f}) ve geri yükseldi"
                break

    return {
        "direction": direction,
        "reason": reason,
        "resistance_zones": [lvl["price"] for lvl in resistance_liquidity],
        "support_zones": [lvl["price"] for lvl in support_liquidity],
    }
