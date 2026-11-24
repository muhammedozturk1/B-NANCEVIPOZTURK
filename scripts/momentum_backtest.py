"""Momentum long-short geçmiş testi: bot ile AYNI momentum.py fonksiyonları, 1s mum yolu, gerçek funding.

Kullanım: TREND_DATA=veri_klasörü python scripts/momentum_backtest.py . sonuc.json [anahtar=değer ...]
  set_mom_require_sign=0|1   işaret şartı (varsayılan 1)
  kill=0|1                   %10 drawdown / %3 günlük limit uygulansın mı (varsayılan 1)
  minnot=0|1                 Binance minimum emir tutarı (BTC 100$, ETH 20$, diğerleri 5$) (varsayılan 1)
  equity=200 start=2021-01-01 end=2026-11-01 reb=8 fee=0.0004 slip=0.0002
"""
import json, math, os, sys
from pathlib import Path
import numpy as np, pandas as pd
REPO, OUT = sys.argv[1], sys.argv[2]
sys.path.insert(0, REPO)
from backend.smcbot import momentum as M  # noqa: E402

D = Path(os.environ.get("TREND_DATA", "data/long"))  # {SYM}_1h.csv + {SYM}_funding.csv (data.binance.vision)
OPT = dict(a.split("=", 1) for a in sys.argv[3:])
START = pd.Timestamp(OPT.get("start", "2021-01-01"), tz="UTC")
END = pd.Timestamp(OPT.get("end", "2026-11-01"), tz="UTC")
EQ0 = float(OPT.get("equity", 200))
MINNOT = OPT.get("minnot", "1") == "1"
KILL = OPT.get("kill", "1") == "1"
REB_H = int(OPT.get("reb", 8))
FEE, SLIP = float(OPT.get("fee", 0.0004)), float(OPT.get("slip", 0.0002))
MIN_NOTIONAL = {"BTCUSDT": 100.0, "ETHUSDT": 20.0}  # diğerleri 5 (yaklaşık; bot borsadan okur)
s = {k[4:] if k.startswith("set_") else k: v for k, v in OPT.items() if k.startswith("set_")}
cfg = M.MomentumConfig.from_settings(s)

idx = pd.date_range("2020-01-01", "2026-11-01", freq="1h", tz="UTC", inclusive="left")
C, Hh, L, V = {}, {}, {}, {}
F = {}
for sym in cfg.universe:
    k = pd.read_csv(D / f"{sym}_1h.csv", header=None, usecols=[0, 2, 3, 4, 7], names=["t", "h", "l", "c", "qv"])
    k = k.drop_duplicates("t")
    k.index = pd.to_datetime(k.t, unit="ms", utc=True)
    k = k.reindex(idx)
    C[sym], Hh[sym], L[sym], V[sym] = k.c.to_numpy(), k.h.to_numpy(), k.l.to_numpy(), k.qv.to_numpy()
    f = pd.read_csv(D / f"{sym}_funding.csv", header=None, usecols=[0, 2], names=["t", "r"])
    f.index = pd.to_datetime(f.t, unit="ms", utc=True)
    F[sym] = f.r.sort_index()
syms = cfg.universe
Cm = np.vstack([pd.Series(C[s]).ffill().to_numpy() for s in syms])   # boşluklar için ileri doldurma (getiri 0)
Hm = np.vstack([np.where(np.isnan(Hh[s]), Cm[i], Hh[s]) for i, s in enumerate(syms)])
Lm = np.vstack([np.where(np.isnan(L[s]), Cm[i], L[s]) for i, s in enumerate(syms)])
Vm = np.vstack([np.nan_to_num(V[s]) for s in syms])
# saatlik funding: o saatin içine düşen funding oranı (saat başı ödenir) ve "bilinen son oran"
fund_hour = np.zeros_like(Cm)
fund_last = np.full_like(Cm, np.nan)
for i, s in enumerate(syms):
    fr = F[s]
    pos = idx.searchsorted(fr.index.floor("1h"))
    ok = pos < len(idx)
    np.add.at(fund_hour[i], pos[ok], fr.to_numpy()[ok])
    last = pd.Series(np.nan, index=idx)
    last.iloc[pos[ok]] = fr.to_numpy()[ok]
    fund_last[i] = last.ffill().to_numpy()

i0, i1 = idx.searchsorted(START), idx.searchsorted(END)
HH = cfg.history_hours
cash = EQ0
qty = np.zeros(len(syms)); entry = np.zeros(len(syms)); stop = np.full(len(syms), np.nan)
blocked = set()
peak = EQ0; day_start_eq = EQ0; paused_day = None; halted_at = None
fees = fund_paid = 0.0; n_orders = 0; n_stops = 0; n_daily = 0; n_kill = 0
eq_curve = []; gross_curve = []; skipped_minnot = 0; rebs = 0; empty_rebs = 0
skip_counts = {}


def equity(px):
    return cash + float(np.sum(qty * (px - entry)))


def trade(j, dq, px):
    """dq coin (işaretli), px fiyat; kayma + komisyon; ortalama giriş güncelle; realize pnl nakde."""
    global cash, fees, n_orders
    side = np.sign(dq)
    fill = px * (1 + SLIP * side)
    fee = abs(dq) * fill * FEE
    fees += fee; cash -= fee; n_orders += 1
    q0 = qty[j]
    if q0 == 0 or np.sign(q0) == side:          # aç / artır
        entry[j] = (entry[j] * abs(q0) + fill * abs(dq)) / (abs(q0) + abs(dq))
        qty[j] = q0 + dq
    else:                                        # azalt / kapat
        close = min(abs(dq), abs(q0))
        cash += close * (fill - entry[j]) * np.sign(q0)
        qty[j] = q0 + dq
        if abs(qty[j]) < 1e-12:
            qty[j] = 0.0; entry[j] = 0.0; stop[j] = np.nan


def close_all(px):
    for j in range(len(syms)):
        if qty[j] != 0:
            trade(j, -qty[j], px[j])


for h in range(i0, i1):
    t = idx[h]
    px_prev = Cm[:, h - 1]
    # --- rebalance (h saatinin açılışında, h-1 kapanışına kadar veriyle)
    if t.hour % REB_H == 0 and halted_at is None and paused_day != t.date():
        rebs += 1
        blocked = set()
        eq = equity(px_prev)
        closes = {s: Cm[i, h - HH:h] for i, s in enumerate(syms)}
        scores = {s: M.score(closes[s], cfg.windows_days) for s in syms}
        vol24 = {s: float(Vm[i, h - 24:h].sum()) for i, s in enumerate(syms)}
        fund = {s: float(fund_last[i, h - 1]) if np.isfinite(fund_last[i, h - 1]) else None for i, s in enumerate(syms)}
        sel = M.select(scores, fund, vol24, cfg)
        for sname, why in sel.skipped.items():
            key = why.split(" ")[0]
            skip_counts[key] = skip_counts.get(key, 0) + 1
        tg = M.target_weights(sel, closes, cfg)
        if MINNOT:
            mn_notes = M.apply_min_notional(tg, eq, {s: MIN_NOTIONAL.get(s, 5.0) for s in syms}, cfg)
            skipped_minnot += sum("açılmadı" in x for x in mn_notes)
        if not tg.weights:
            empty_rebs += 1
        cur = {s: float(qty[i] * px_prev[i]) for i, s in enumerate(syms) if qty[i] != 0}
        orders = M.plan_orders(tg.weights, cur, eq, {s: px_prev[i] for i, s in enumerate(syms)},
                               {s: MIN_NOTIONAL.get(s, 5.0) if MINNOT else 0.0 for s in syms}, cfg)
        for o in orders:
            j = syms.index(o["symbol"])
            trade(j, o["delta"] / px_prev[j], px_prev[j])
            if qty[j] != 0:
                stop[j] = entry[j] * (1 - np.sign(qty[j]) * tg.stop_pct.get(o["symbol"], 0.12))
    # --- saat içi: koruma stopu (önce, kötü sıra)
    for j in range(len(syms)):
        if qty[j] == 0 or not np.isfinite(stop[j]):
            continue
        hit = (Lm[j, h] <= stop[j]) if qty[j] > 0 else (Hm[j, h] >= stop[j])
        if hit:
            o = Cm[j, h - 1]
            px = min(o, stop[j]) if qty[j] > 0 else max(o, stop[j])   # gap: kötü olan
            trade(j, -qty[j], px); n_stops += 1
    # --- funding (saat başı ödenen)
    px_now = Cm[:, h]
    fpay = float(np.sum(qty * px_now * fund_hour[:, h]))
    cash -= fpay; fund_paid += fpay
    eq = equity(px_now)
    # --- risk: günlük kayıp, max drawdown
    if t.hour == 0:
        day_start_eq = equity(px_prev) if h > i0 else EQ0
    if KILL and halted_at is None:
        if eq <= peak * 0.90:
            close_all(px_now); halted_at = str(t); n_kill += 1
        elif eq <= day_start_eq * 0.97 and paused_day != t.date():
            close_all(px_now); paused_day = t.date(); n_daily += 1
    eq = equity(px_now)
    peak = max(peak, eq)
    eq_curve.append(eq)
    gross_curve.append(float(np.sum(np.abs(qty * px_now))) / max(eq, 1e-9))

eq = pd.Series(eq_curve, index=idx[i0:i1])
daily = eq.resample("1D").last()
dr = daily.pct_change().dropna()
yrs = (eq.index[-1] - eq.index[0]).days / 365.25
res = {
    "opt": OPT, "final": round(float(eq.iloc[-1]), 2), "toplam%": round(100 * (eq.iloc[-1] / EQ0 - 1), 1),
    "CAGR%": round(100 * ((eq.iloc[-1] / EQ0) ** (1 / yrs) - 1), 2) if eq.iloc[-1] > 0 else -100,
    "Sharpe": round(float(dr.mean() / dr.std() * math.sqrt(365)), 2) if dr.std() > 0 else 0,
    "maxDD%": round(100 * float((eq / eq.cummax() - 1).min()), 1),
    "yıllık_vol%": round(100 * float(dr.std() * math.sqrt(365)), 1),
    "ort_kaldıraç": round(float(np.mean(gross_curve)), 3), "emir": n_orders, "stop": n_stops,
    "komisyon$": round(fees, 2), "funding$": round(fund_paid, 2), "günlük_limit": n_daily, "kill": n_kill,
    "durdu": halted_at, "rebalance": rebs, "boş_rebalance": empty_rebs, "min_notional_atlanan": skipped_minnot,
    "atlama_nedenleri": skip_counts,
    "yıllar": {str(y): round(100 * (g.iloc[-1] / g.iloc[0] - 1), 1) for y, g in eq.groupby(eq.index.year)},
}
Path(OUT).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
print(json.dumps({k: v for k, v in res.items() if k not in ("opt",)}, ensure_ascii=False, default=str))
