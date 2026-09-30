"""
Özellik (feature) çıkarımı.

Her mum için, SADECE o muma kadarki (o mum dahil, kapanmış) veriyle hesaplanan
göstergeler üretilir. Üst zaman dilimi (confirm) verisi, giriş mumunun
kapanış anında KAPANMIŞ olan son üst-TF mumundan alınır (geleceğe bakma yok).

Model için özellikler "yöne göre aynalanır": long için +, short için - ile
çarpılır. Böylece model long ve short'u aynı desen olarak öğrenir, veri iki
katına çıkmış gibi olur.
"""
import numpy as np
import pandas as pd
from backend.strategies import indicators as ind

SETUP_NAMES = ["trend_pullback", "range_reversion", "liquidity_sweep"]

# Modelin gördüğü sütunlar (sıra önemli - model dosyasına da yazılır)
MODEL_FEATURES = [
    "side",
    "atr_pct", "adx", "bb_width", "vol_z", "hour_sin", "hour_cos", "c_adx",
    "d_ret_1", "d_ret_3", "d_ret_12", "d_rsi", "d_dist_ema21", "d_dist_ema50",
    "d_ema21_50", "d_ema50_slope", "d_bb_z", "d_vwap_dev", "d_range_pos",
    "d_body", "d_wick_with", "d_wick_against", "d_pivot_dist",
    "d_c_trend", "d_c_rsi", "d_c_dist_ema55",
] + [f"setup_{s}" for s in SETUP_NAMES]


def build_frame(entry_ohlcv, confirm_ohlcv, entry_tf: str, confirm_tf: str) -> pd.DataFrame:
    """Ham göstergeleri içeren tam tabloyu döner (kurulum tespiti + model özellikleri için)."""
    df = ind.to_df(entry_ohlcv)
    c, o, h, l, v = df["close"], df["open"], df["high"], df["low"], df["volume"]
    tf_ms = ind.TF_MS[entry_tf]

    df["atr"] = ind.atr(df)
    a = df["atr"].replace(0, np.nan)
    df["atr_pct"] = df["atr"] / c
    df["ema9"] = ind.ema(c, 9)
    df["ema21"] = ind.ema(c, 21)
    df["ema50"] = ind.ema(c, 50)
    df["rsi"] = ind.rsi(c)
    df["adx"] = ind.adx(df)

    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_z"] = (c - sma20) / std20.replace(0, np.nan)
    df["bb_width"] = 4 * std20 / sma20

    df["vwap"] = ind.session_vwap(df)
    piv = ind.prev_day_pivots(df, tf_ms)
    df = pd.concat([df, piv], axis=1)

    vmean, vstd = v.rolling(20).mean(), v.rolling(20).std()
    df["vol_z"] = (v - vmean) / vstd.replace(0, np.nan)

    hi20 = h.rolling(20).max()
    lo20 = l.rolling(20).min()
    df["range_pos"] = (c - lo20) / (hi20 - lo20).replace(0, np.nan)
    # önceki 20 mumun (mevcut hariç) tepe/dip seviyesi - likidite havuzu
    df["prior_high20"] = h.shift(1).rolling(20).max()
    df["prior_low20"] = l.shift(1).rolling(20).min()

    df["ret_1"] = c.pct_change(1) / df["atr_pct"]
    df["ret_3"] = c.pct_change(3) / df["atr_pct"]
    df["ret_12"] = c.pct_change(12) / df["atr_pct"]
    df["dist_ema21"] = (c - df["ema21"]) / a
    df["dist_ema50"] = (c - df["ema50"]) / a
    df["ema21_50"] = (df["ema21"] - df["ema50"]) / a
    df["ema50_slope"] = (df["ema50"] - df["ema50"].shift(5)) / a
    df["vwap_dev"] = (c - df["vwap"]) / a
    df["pivot_dist"] = (c - df["pivot"]) / a
    df["body"] = (c - o) / a
    df["upper_wick"] = (h - np.maximum(o, c)) / a
    df["lower_wick"] = (np.minimum(o, c) - l) / a

    close_time = df["ts"] + tf_ms
    hours = (close_time // 3_600_000) % 24
    df["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    df["hour_cos"] = np.cos(2 * np.pi * hours / 24)

    # --- Üst zaman dilimi ---
    cdf = ind.to_df(confirm_ohlcv)
    cdf["c_ema21"] = ind.ema(cdf["close"], 21)
    cdf["c_ema55"] = ind.ema(cdf["close"], 55)
    cdf["c_rsi"] = ind.rsi(cdf["close"])
    cdf["c_adx"] = ind.adx(cdf)
    cdf["c_atr"] = ind.atr(cdf)
    cdf["c_close_time"] = cdf["ts"] + ind.TF_MS[confirm_tf]
    cdf = cdf[["c_close_time", "c_ema21", "c_ema55", "c_rsi", "c_adx", "c_atr"]]

    df["close_time"] = close_time
    df = pd.merge_asof(df.sort_values("close_time"), cdf.sort_values("c_close_time"),
                       left_on="close_time", right_on="c_close_time", direction="backward")
    df["c_trend"] = (df["c_ema21"] - df["c_ema55"]) / df["c_atr"].replace(0, np.nan)
    df["c_dist_ema55"] = (df["close"] - df["c_ema55"]) / df["c_atr"].replace(0, np.nan)
    return df


def model_vector(row, side: int, setup: str) -> dict:
    """Tek bir mum satırı + yön + kurulum için model girdi sözlüğü üretir."""
    s = float(side)
    wick_with = row["lower_wick"] if side > 0 else row["upper_wick"]
    wick_against = row["upper_wick"] if side > 0 else row["lower_wick"]
    vec = {
        "side": s,
        "atr_pct": row["atr_pct"], "adx": row["adx"], "bb_width": row["bb_width"],
        "vol_z": row["vol_z"], "hour_sin": row["hour_sin"], "hour_cos": row["hour_cos"],
        "c_adx": row["c_adx"],
        "d_ret_1": s * row["ret_1"], "d_ret_3": s * row["ret_3"], "d_ret_12": s * row["ret_12"],
        "d_rsi": s * (row["rsi"] - 50),
        "d_dist_ema21": s * row["dist_ema21"], "d_dist_ema50": s * row["dist_ema50"],
        "d_ema21_50": s * row["ema21_50"], "d_ema50_slope": s * row["ema50_slope"],
        "d_bb_z": s * row["bb_z"], "d_vwap_dev": s * row["vwap_dev"],
        "d_range_pos": s * (row["range_pos"] - 0.5),
        "d_body": s * row["body"], "d_wick_with": wick_with, "d_wick_against": wick_against,
        "d_pivot_dist": s * row["pivot_dist"],
        "d_c_trend": s * row["c_trend"], "d_c_rsi": s * (row["c_rsi"] - 50),
        "d_c_dist_ema55": s * row["c_dist_ema55"],
    }
    for name in SETUP_NAMES:
        vec[f"setup_{name}"] = 1.0 if setup == name else 0.0
    return {k: (float(v) if v is not None else np.nan) for k, v in vec.items()}


def model_matrix(frame: pd.DataFrame, sides: np.ndarray, setups: np.ndarray) -> pd.DataFrame:
    """Backtest için vektörel versiyon (model_vector ile birebir aynı mantık)."""
    s = sides.astype(float)
    long_ = s > 0
    out = pd.DataFrame(index=frame.index)
    out["side"] = s
    for col in ["atr_pct", "adx", "bb_width", "vol_z", "hour_sin", "hour_cos", "c_adx"]:
        out[col] = frame[col].values
    for col in ["ret_1", "ret_3", "ret_12", "dist_ema21", "dist_ema50", "ema21_50",
                "ema50_slope", "bb_z", "vwap_dev", "body", "pivot_dist", "c_trend", "c_dist_ema55"]:
        out[f"d_{col}"] = s * frame[col].values
    out["d_rsi"] = s * (frame["rsi"].values - 50)
    out["d_range_pos"] = s * (frame["range_pos"].values - 0.5)
    out["d_wick_with"] = np.where(long_, frame["lower_wick"].values, frame["upper_wick"].values)
    out["d_wick_against"] = np.where(long_, frame["upper_wick"].values, frame["lower_wick"].values)
    out["d_c_rsi"] = s * (frame["c_rsi"].values - 50)
    for name in SETUP_NAMES:
        out[f"setup_{name}"] = (setups == name).astype(float)
    return out[MODEL_FEATURES]
