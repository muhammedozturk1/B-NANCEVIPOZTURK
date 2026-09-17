"""
EMA (Üstel Hareketli Ortalama) stratejisi.
İki EMA'nın kesişimi ile trend yönü belirlenir.
"""
import pandas as pd


def calculate_ema(closes: list, period: int) -> pd.Series:
    return pd.Series(closes).ewm(span=period, adjust=False).mean()


def get_signal(ohlcv: list, fast_period: int = 9, slow_period: int = 21) -> dict:
    """
    ohlcv: ccxt formatında [[timestamp, open, high, low, close, volume], ...]
    Dönüş: {"direction": "long"/"short"/"neutral", "fast": float, "slow": float}
    """
    closes = [candle[4] for candle in ohlcv]
    if len(closes) < slow_period + 2:
        return {"direction": "neutral", "reason": "yetersiz veri"}

    ema_fast = calculate_ema(closes, fast_period)
    ema_slow = calculate_ema(closes, slow_period)

    fast_now, fast_prev = ema_fast.iloc[-1], ema_fast.iloc[-2]
    slow_now, slow_prev = ema_slow.iloc[-1], ema_slow.iloc[-2]

    # Altın kesişim (fast, slow'u yukarı keser) -> long
    if fast_prev <= slow_prev and fast_now > slow_now:
        direction = "long"
    # Ölüm kesişimi -> short
    elif fast_prev >= slow_prev and fast_now < slow_now:
        direction = "short"
    # Kesişim yoksa mevcut trend yönü zayıf sinyal olarak değerlendirilir
    elif fast_now > slow_now:
        direction = "long_weak"
    elif fast_now < slow_now:
        direction = "short_weak"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "fast": round(float(fast_now), 6),
        "slow": round(float(slow_now), 6),
    }
