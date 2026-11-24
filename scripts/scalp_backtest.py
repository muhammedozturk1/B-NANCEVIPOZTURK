"""Scalp şartnamesi geçmiş testi (BTCUSDT, ETHUSDT 1 dk, Mayıs–Kasım 2026). Bot ile aynı scalp.py fonksiyonları.
Kullanım: bt.py REPO OUT.json [fee=0.0005 slip=0.0002 equity=200 ...]"""
import json, os, sys
from pathlib import Path
import numpy as np, pandas as pd
REPO, OUT = sys.argv[1], sys.argv[2]
sys.path.insert(0, REPO)
from backend.smc.core.types import Candle, Side  # noqa: E402
from backend.smc.strategy import scalp as S  # noqa: E402
OPT = dict(a.split("=", 1) for a in sys.argv[3:])
FEE, SLIP, EQ0 = float(OPT.get("fee", 0.0005)), float(OPT.get("slip", 0.0002)), float(OPT.get("equity", 200))
TP_SLIP = float(OPT.get("tpslip", SLIP))
D = Path(os.environ.get("SCALP_DATA", "data/m6"))  # {SYM}_1m.csv (data.binance.vision 1 dk kline)
P = S.ScalpParams(**{k[2:]: type(getattr(S.ScalpParams(), k[2:]))(v) for k, v in OPT.items() if k.startswith("p_")})


def load(sym):
    k = pd.read_csv(D / f"{sym}_1m.csv", header=None, usecols=[0, 1, 2, 3, 4, 5], names=["t", "o", "h", "l", "c", "v"])
    k = k.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    return k


def candles_1m(k, a, b):
    return [Candle(int(t), o, h, l, c, v, int(t) + 59_999) for t, o, h, l, c, v in
            zip(k.t.values[a:b], k.o.values[a:b], k.h.values[a:b], k.l.values[a:b], k.c.values[a:b], k.v.values[a:b])]


def signals(sym):
    k = load(sym)
    t, o, h, l, c, v = (k[x].to_numpy() for x in "tohlcv")
    # 5 dk mumlar (yalnız tam 5 adet 1 dk içeren)
    g = (t // 300_000) * 300_000
    df = pd.DataFrame({"g": g, "o": o, "h": h, "l": l, "c": c, "v": v})
    agg = df.groupby("g").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"), n=("c", "size"))
    agg = agg[agg.n == 5]
    t5 = agg.index.to_numpy()
    c5 = [Candle(int(tt), r.o, r.h, r.l, r.c, r.v, int(tt) + 299_999) for tt, r in zip(t5, agg.itertuples())]
    out = []
    n = len(t)
    hi6 = pd.Series(h).rolling(6).max().shift(1).to_numpy()
    lo6 = pd.Series(l).rolling(6).min().shift(1).to_numpy()
    cand = np.where((c > hi6) | (c < lo6))[0]
    for i in cand:
        if i < 60 or i >= n - 8:
            continue
        close_ms = int(t[i]) + 60_000
        j5 = int(np.searchsorted(t5 + 300_000, close_ms, side="right"))  # kapanmış 5 dk mum sayısı
        if j5 < 100:
            continue
        ev = S.evaluate_scalp(sym, c5[j5 - 100:j5], candles_1m(k, i - 59, i + 1), P)
        if ev is not None and ev.setup is not None:
            out.append((i, ev))
    return k, out


def simulate():
    data = {s: signals(s) for s in ("BTCUSDT", "ETHUSDT")}
    events = sorted(((int(data[s][0].t.values[i]) + 60_000, s, i, ev) for s in data for i, ev in data[s][1]),
                    key=lambda x: x[0])
    eq = EQ0
    busy_until = 0
    trades = []
    skipped = {"busy": 0, "plan": 0}
    for t_sig, sym, i, ev in events:
        if t_sig < busy_until:
            skipped["busy"] += 1
            continue
        k = data[sym][0]
        o, h, l, c, v = (k[x].to_numpy() for x in "ohlcv")
        side = ev.side
        sg = 1 if side is Side.LONG else -1
        j0 = i + 1
        fill = o[j0] * (1 + sg * SLIP)
        stop = ev.setup.stop
        plan = S.plan_from_fill(side, fill, stop, P, eq)
        if plan is None:
            skipped["plan"] += 1
            continue
        qty, tp, be = plan["qty"], plan["tp"], plan["be_trigger"]
        cur_stop = stop
        be_done = False
        ex = reason = None
        mfe = 0.0
        for j in range(j0, min(j0 + P.max_hold_min, len(o))):
            if (o[j] - cur_stop) * sg <= 0 and j > j0:
                ex, reason = o[j] * (1 - sg * SLIP), "stop_gap"
            elif (l[j] if sg > 0 else h[j]) * sg <= cur_stop * sg:
                ex, reason = cur_stop * (1 - sg * SLIP), "breakeven" if be_done else "stop"
            elif (h[j] if sg > 0 else l[j]) * sg >= tp * sg:
                ex, reason = tp * (1 - sg * TP_SLIP), "tp"
            if ex is not None:
                break
            mfe = max(mfe, ((h[j] if sg > 0 else l[j]) - fill) * sg / abs(fill - stop))
            if not be_done and (h[j] if sg > 0 else l[j]) * sg >= be * sg:
                be_done, cur_stop = True, fill               # bir sonraki mumdan itibaren
            prev = candles_1m(k, j - 15, j)
            if S.strong_opposite(candles_1m(k, j, j + 1)[0], prev, side, P):
                ex, reason = c[j] * (1 - sg * SLIP), "opposite"
                break
        else:
            j = min(j0 + P.max_hold_min, len(o)) - 1
            ex, reason = c[j] * (1 - sg * SLIP), "time"
        gross = (ex - fill) * sg * qty
        fee = (fill + ex) * qty * FEE
        pnl = gross - fee
        eq += pnl
        busy_until = int(k.t.values[j]) + 60_000
        trades.append({"t": int(k.t.values[j0]), "sym": sym, "side": side.value, "entry": fill, "stop": stop, "tp": tp,
                       "exit": ex, "reason": reason, "mins": j - j0 + 1, "gross": gross, "fee": fee, "pnl": pnl,
                       "r": pnl / P.risk_usdt, "stop_pct": plan["stop_pct"], "notional": plan["notional"]})
    return trades, skipped, eq


trades, skipped, eq = simulate()
df = pd.DataFrame(trades)
eqc = EQ0 + df.pnl.cumsum()
dd = float((eqc - np.maximum.accumulate(np.r_[EQ0, eqc.to_numpy()])[1:]).min())
wins = df.pnl > 0
res = {
    "opt": OPT, "işlem": len(df), "kazanma%": round(100 * wins.mean(), 1), "net$": round(df.pnl.sum(), 2),
    "brüt$": round(df.gross.sum(), 2), "komisyon$": round(df.fee.sum(), 2), "son_bakiye": round(eq, 2),
    "ort_R": round(df.r.mean(), 3), "PF": round(df.pnl[wins].sum() / -df.pnl[~wins].sum(), 3),
    "maxDD$": round(dd, 2), "ort_stop%": round(100 * df.stop_pct.mean(), 3), "ort_nominal$": round(df.notional.mean(), 0),
    "ort_süre_dk": round(df.mins.mean(), 2), "günlük_işlem": round(len(df) / ((df.t.max() - df.t.min()) / 86_400_000), 2),
    "çıkışlar": {r: {"n": int((df.reason == r).sum()), "ort_$": round(df.pnl[df.reason == r].mean(), 3)} for r in df.reason.unique()},
    "aylar": {m: round(g.pnl.sum(), 2) for m, g in df.groupby(pd.to_datetime(df.t, unit="ms").dt.strftime("%Y-%m"))},
    "semboller": {s: {"n": int(len(g)), "net$": round(g.pnl.sum(), 2)} for s, g in df.groupby("sym")},
    "atlanan": skipped,
}
Path(OUT).write_text(json.dumps(res, ensure_ascii=False, indent=1))
df.to_csv(OUT.replace(".json", "_trades.csv"), index=False)
print(json.dumps(res, ensure_ascii=False))
