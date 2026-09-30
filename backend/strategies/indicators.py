"""
Teknik göstergeler - hepsi vektörel (pandas) ve SADECE KAPANMIŞ mumlarla çalışır.

Aynı fonksiyonlar hem canlı botta hem backtest/AI eğitiminde kullanılır;
böylece "backtest'te başka, canlıda başka hesap" sorunu olmaz.
"""
import time
import numpy as np
import pandas as pd

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}
DAY_MS = 86_400_000


def to_df(ohlcv) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def drop_unclosed(ohlcv: list, timeframe: str, now_ms: int = None) -> list:
    """Borsa, son eleman olarak henüz KAPANMAMIŞ mumu döndürür. Onunla karar
    vermek sinyalin mum kapanınca kaybolmasına (repaint) yol açar - atıyoruz."""
    if not ohlcv:
        return ohlcv
    now_ms = now_ms or int(time.time() * 1000)
    if ohlcv[-1][0] + TF_MS[timeframe] > now_ms:
        return ohlcv[:-1]
    return ohlcv


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift()
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr_ = atr(df, n).replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Her UTC gününün başında SIFIRLANAN VWAP (eski kod 200 mumu kümülatif
    topluyordu; 4h grafikte bu ~33 günlük anlamsız bir ortalamaydı)."""
    day = df["ts"] // DAY_MS
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return pv / vol


def prev_day_pivots(df: pd.DataFrame, tf_ms: int) -> pd.DataFrame:
    """Klasik pivotlar ÖNCEKİ TAM UTC GÜNÜNÜN H/L/C'sinden hesaplanır.
    (Eski kod pivotu bir önceki TEK mumdan hesaplıyordu - fiyat neredeyse
    her zaman S1'e yakın çıktığı için sürekli 'long' sinyali üretiyordu.)"""
    day = df["ts"] // DAY_MS
    daily = df.groupby(day).agg(high=("high", "max"), low=("low", "min"),
                                close=("close", "last"), n=("close", "size"))
    expected = DAY_MS // tf_ms
    daily.loc[daily["n"] < expected * 0.95, ["high", "low", "close"]] = np.nan  # eksik gün
    prev = daily.shift(1)
    p = (prev["high"] + prev["low"] + prev["close"]) / 3
    out = pd.DataFrame({
        "pivot": p,
        "r1": 2 * p - prev["low"],
        "s1": 2 * p - prev["high"],
    })
    return out.reindex(day.values).reset_index(drop=True).set_index(df.index)
