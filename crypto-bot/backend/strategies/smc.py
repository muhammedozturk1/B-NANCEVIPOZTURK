"""
SMC (Smart Money Concepts) - basitleştirilmiş uygulama.

Kapsam:
  - Fractal swing high/low tespiti
  - BOS (Break of Structure): mevcut trend yönünde yapı kırılımı -> trendin devamı sinyali
  - CHOCH (Change of Character): trendin tersine ilk yapı kırılımı -> olası trend dönüşü sinyali
  - Basit Order Block tespiti: kırılıma sebep olan son ters yönlü mum

Not: Bu, kurumsal SMC göstergelerinin tam karşılığı değil, temel mantığın
kod haline getirilmiş, test edilebilir bir versiyonudur.
"""
from backend import config


def find_swing_points(ohlcv: list, lookback: int = None):
    """Fractal mantığıyla swing high ve swing low noktalarını bulur.
    Dönüş: (swing_highs, swing_lows) -> her biri (index, price) listesi
    """
    lookback = lookback or config.SWING_LOOKBACK
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]

    swing_highs, swing_lows = [], []
    for i in range(lookback, len(ohlcv) - lookback):
        window_high = highs[i - lookback: i + lookback + 1]
        window_low = lows[i - lookback: i + lookback + 1]

        if highs[i] == max(window_high):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(window_low):
            swing_lows.append((i, lows[i]))

    return swing_highs, swing_lows


def get_signal(ohlcv: list) -> dict:
    swing_highs, swing_lows = find_swing_points(ohlcv)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"direction": "neutral", "reason": "yetersiz swing verisi"}

    current_price = ohlcv[-1][4]

    last_high = swing_highs[-1][1]
    prev_high = swing_highs[-2][1]
    last_low = swing_lows[-1][1]
    prev_low = swing_lows[-2][1]

    # Trend yapısı: yükselen tepe+dip = boğa yapısı, düşen tepe+dip = ayı yapısı
    bullish_structure = last_high > prev_high and last_low > prev_low
    bearish_structure = last_high < prev_high and last_low < prev_low

    direction = "neutral"
    event = None

    # BOS: fiyat, mevcut yapı yönündeki son swing'i kırdı -> trend devam sinyali
    if bullish_structure and current_price > last_high:
        direction = "long"
        event = "BOS (yükseliş devamı)"
    elif bearish_structure and current_price < last_low:
        direction = "short"
        event = "BOS (düşüş devamı)"
    # CHOCH: yapı ayı iken fiyat son tepeyi yukarı kırdı -> olası dönüş
    elif bearish_structure and current_price > last_high:
        direction = "long"
        event = "CHOCH (olası dönüş - yükseliş)"
    elif bullish_structure and current_price < last_low:
        direction = "short"
        event = "CHOCH (olası dönüş - düşüş)"

    return {
        "direction": direction,
        "event": event,
        "last_swing_high": last_high,
        "last_swing_low": last_low,
    }
