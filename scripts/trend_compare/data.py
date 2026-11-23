"""20 coin 1s mum (2020-01..2026-10) -> Candles 1h ve 4h, funding sözlüğü."""
import os

import numpy as np
import pandas as pd
from pathlib import Path

D = Path(os.environ.get("TREND_DATA", "data/long"))  # {SYM}_1h.csv ve {SYM}_funding.csv (data.binance.vision)
SYMS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT", "LINKUSDT", "BCHUSDT",
        "XLMUSDT", "DOGEUSDT", "UNIUSDT", "SOLUSDT", "AVAXUSDT", "TRXUSDT", "ETCUSDT", "ATOMUSDT", "FILUSDT",
        "LUNAUSDT", "FTTUSDT"]


def load(sym, Candles):
    k = pd.read_csv(D / f"{sym}_1h.csv", header=None, usecols=[0, 1, 2, 3, 4, 5, 9],
                    names=["t", "o", "h", "l", "c", "v", "tb"])
    if k.empty:
        return None
    k = k.drop_duplicates("t").sort_values("t")
    # 1h ızgarasını tamamla (eksik saatler: veri yok -> atla)
    c1 = Candles(k.t.to_numpy(np.int64), k.o.to_numpy(float), k.h.to_numpy(float), k.l.to_numpy(float),
                 k.c.to_numpy(float), k.v.to_numpy(float), k.tb.to_numpy(float), 3_600_000)
    k.index = pd.to_datetime(k.t, unit="ms", utc=True)
    r = k.resample("4h", label="left", closed="left")
    b = pd.DataFrame({"o": r.o.first(), "h": r.h.max(), "l": r.l.min(), "c": r.c.last(), "v": r.v.sum(),
                      "tb": r.tb.sum(), "n": r.c.count()}).dropna()
    b = b[b.n == 4]  # yalnız tam 4s mumlar
    t4 = (b.index.astype("int64") // 1_000_000).to_numpy(np.int64)
    c4 = Candles(t4, b.o.to_numpy(float), b.h.to_numpy(float), b.l.to_numpy(float), b.c.to_numpy(float),
                 b.v.to_numpy(float), b.tb.to_numpy(float), 14_400_000)
    f = pd.read_csv(D / f"{sym}_funding.csv", header=None, usecols=[0, 2], names=["t", "r"])
    fund = (f.t.to_numpy(np.int64), f.r.to_numpy(float))
    return c1, c4, fund
