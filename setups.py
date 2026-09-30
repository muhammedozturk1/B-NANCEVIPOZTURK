"""
Giriş kurulumları (setups) - "aday sinyal" üretir, son kararı AI filtresi verir.

Eski sistemdeki sorun: trend takip eden (EMA, VWAP) ve ters işlem yapan
(destek/direnç, likidite, korku-açgözlülük) teknikler aynı sandıkta oylanıyordu,
birbirini götürüyordu. Artık her kurulum kendi PİYASA REJİMİNDE çalışır:

  trend_pullback  : Üst TF trendli iken, trend yönünde EMA21'e geri çekilmeden dönüş
  range_reversion : Trend yokken (ADX < 20) Bollinger bandı dışından içeri dönüş
  liquidity_sweep : Son 20 mumun tepe/dibini süpürüp geri kapanma (stop avı dönüşü)
"""
import numpy as np
import pandas as pd

ADX_RANGE_MAX = 20
ADX_TREND_MIN = 18


def _trend_pullback(f: pd.DataFrame) -> pd.Series:
    up = (f["c_ema21"] > f["c_ema55"]) & (f["ema21"] > f["ema50"]) & (f["adx"] >= ADX_TREND_MIN)
    dn = (f["c_ema21"] < f["c_ema55"]) & (f["ema21"] < f["ema50"]) & (f["adx"] >= ADX_TREND_MIN)
    long_ = up & (f["low"] <= f["ema21"]) & (f["close"] > f["ema21"]) & (f["close"] > f["open"]) \
        & f["rsi"].between(40, 65)
    short = dn & (f["high"] >= f["ema21"]) & (f["close"] < f["ema21"]) & (f["close"] < f["open"]) \
        & f["rsi"].between(35, 60)
    return pd.Series(np.where(long_, 1, np.where(short, -1, 0)), index=f.index)


def _range_reversion(f: pd.DataFrame) -> pd.Series:
    ranging = f["adx"] < ADX_RANGE_MAX
    prev_z = f["bb_z"].shift(1)
    long_ = ranging & (prev_z < -2) & (f["bb_z"] > -2) & (f["close"] > f["open"])
    short = ranging & (prev_z > 2) & (f["bb_z"] < 2) & (f["close"] < f["open"])
    return pd.Series(np.where(long_, 1, np.where(short, -1, 0)), index=f.index)


def _liquidity_sweep(f: pd.DataFrame) -> pd.Series:
    long_ = (f["low"] < f["prior_low20"]) & (f["close"] > f["prior_low20"]) & (f["lower_wick"] > 0.5)
    short = (f["high"] > f["prior_high20"]) & (f["close"] < f["prior_high20"]) & (f["upper_wick"] > 0.5)
    return pd.Series(np.where(long_, 1, np.where(short, -1, 0)), index=f.index)


SETUP_FUNCS = {
    "trend_pullback": _trend_pullback,
    "range_reversion": _range_reversion,
    "liquidity_sweep": _liquidity_sweep,
}


def detect(frame: pd.DataFrame, setup_names: list):
    """Her mum için (side, setup) döner. Listedeki ilk tetiklenen kurulum kazanır;
    iki kurulum zıt yön verirse o mum atlanır."""
    sides = np.zeros(len(frame), dtype=int)
    names = np.array([""] * len(frame), dtype=object)
    conflict = np.zeros(len(frame), dtype=bool)
    for name in setup_names:
        sig = SETUP_FUNCS[name](frame).values
        conflict |= (sides != 0) & (sig != 0) & (sig != sides)
        take = (sides == 0) & (sig != 0)
        sides[take] = sig[take]
        names[take] = name
    sides[conflict] = 0
    names[conflict] = ""
    return sides, names
