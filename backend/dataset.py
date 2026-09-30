"""Geçmiş veriden (özellik + gerçekleşen net R) eğitim tablosu üretir."""
import numpy as np
import pandas as pd
from backend import config
from backend.strategies import features as feat
from backend.strategies import setups as st
from backend.strategies.indicators import TF_MS
from backend.ai.simulate import simulate

WARMUP_BARS = 300


def build_symbol_dataset(symbol: str, entry_ohlcv, confirm_ohlcv, engine: str) -> pd.DataFrame:
    tfs = config.ENGINE_TIMEFRAMES[engine]
    frame = feat.build_frame(entry_ohlcv, confirm_ohlcv, tfs["entry"], tfs["confirm"])
    sides, names = st.detect(frame, config.ENGINE_SETUPS[engine])
    sides[:WARMUP_BARS] = 0

    idx = np.nonzero(sides)[0]
    if len(idx) == 0:
        return pd.DataFrame()

    net_r, bars, reason = simulate(
        frame["open"].values, frame["high"].values, frame["low"].values, frame["close"].values,
        frame["atr"].values, idx, sides[idx], config.EXIT_PARAMS[engine], config.COST_PER_SIDE,
    )
    X = feat.model_matrix(frame.iloc[idx].reset_index(drop=True), sides[idx], names[idx])
    tf_ms = TF_MS[tfs["entry"]]
    out = X.copy()
    out["symbol"] = symbol
    out["setup"] = names[idx]
    out["bar_idx"] = idx
    out["ts"] = frame["ts"].values[idx]
    out["exit_ts"] = out["ts"] + (bars + 1) * tf_ms
    out["net_r"] = net_r
    out["reason"] = reason
    out = out[np.isfinite(out["net_r"])]
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=feat.MODEL_FEATURES)
    return out.reset_index(drop=True)


def sequential_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Canlıdaki gibi: aynı sembolde açık işlem varken yenisi açılamaz."""
    df = df.sort_values("ts")
    keep = []
    busy_until = {}
    for i, row in zip(df.index, df[["symbol", "ts", "exit_ts"]].itertuples(index=False)):
        if row.ts >= busy_until.get(row.symbol, -1):
            keep.append(i)
            busy_until[row.symbol] = row.exit_ts
    return df.loc[keep]


def performance(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"trades": 0}
    r = df.sort_values("ts")["net_r"].values
    wins, losses = r[r > 0], r[r <= 0]
    equity = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], equity]))[1:] - equity)) if len(r) else 0.0
    days = max((df["ts"].max() - df["ts"].min()) / 86_400_000, 1)
    return {
        "trades": int(len(r)),
        "win_rate": round(float(len(wins) / len(r)), 3),
        "avg_r": round(float(r.mean()), 3),
        "total_r": round(float(r.sum()), 2),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
        "max_drawdown_r": round(dd, 2),
        "trades_per_day": round(float(len(r) / days), 2),
    }
