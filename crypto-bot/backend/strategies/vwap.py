"""
VWAP (Hacim Ağırlıklı Ortalama Fiyat) stratejisi.
Fiyat VWAP'ın üstündeyse alıcı baskısı, altındaysa satıcı baskısı yorumlanır.
Sapma büyüdükçe (aşırı uzaklaşma) ortalamaya dönüş beklentisi de sinyale eklenir.
"""
import pandas as pd


def calculate_vwap(ohlcv: list) -> float:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_pv = (typical_price * df["volume"]).cumsum()
    cumulative_vol = df["volume"].cumsum()
    vwap = cumulative_pv / cumulative_vol
    return float(vwap.iloc[-1])


def get_signal(ohlcv: list, deviation_threshold: float = 0.004) -> dict:
    """
    deviation_threshold: fiyatın VWAP'tan yüzde kaç sapınca "aşırı sapma" sayılacağı (varsayılan %0.4)
    """
    if len(ohlcv) < 10:
        return {"direction": "neutral", "reason": "yetersiz veri"}

    vwap = calculate_vwap(ohlcv)
    current_price = ohlcv[-1][4]  # son kapanış

    deviation = (current_price - vwap) / vwap

    if deviation > deviation_threshold:
        # Fiyat VWAP'ın belirgin üstünde -> güçlü alıcı bölgesi, long teyidi
        direction = "long"
    elif deviation < -deviation_threshold:
        direction = "short"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "vwap": round(vwap, 6),
        "price": current_price,
        "deviation_percent": round(deviation * 100, 3),
    }
