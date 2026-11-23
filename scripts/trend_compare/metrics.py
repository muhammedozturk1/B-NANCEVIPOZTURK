import json, os, pickle, sys
import numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1])
from backend.smcbot import config
from backend.smcbot.backtest import portfolio
ms = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp() * 1000)
PER = {"IS 2020-2023": (ms("2020-01-01"), ms("2024-01-01")), "OOS 2024-2026": (ms("2024-01-01"), ms("2026-11-01")),
       "TÜMÜ": (0, ms("2030-01-01"))}
PS = dict(config.DEFAULT_SETTINGS, strategy="trend", risk_per_trade_pct=1.0, max_open_positions=3, max_same_direction=2,
          trend_max_new_per_bar=1)


def raw_metrics(tr):
    r = np.array([t["r_multiple"] for t in tr])
    if not len(r):
        return {}
    srt = sorted(tr, key=lambda t: t["exit_time"])
    cum = np.cumsum([t["r_multiple"] for t in srt])
    dd = float(np.max(np.maximum.accumulate(np.r_[0, cum]) - np.r_[0, cum]))
    mfe = np.array([t["mfe"] for t in tr])
    big = mfe >= 1.0
    give = float(np.mean((mfe[big] - r[big]) / mfe[big])) if big.any() else None
    give_med = float(np.median((mfe[big] - r[big]) / mfe[big])) if big.any() else None
    return {"işlem": len(r), "net_R": round(float(r.sum()), 1), "beklenti_R": round(float(r.mean()), 4),
            "PF": round(float(r[r > 0].sum() / -r[r < 0].sum()), 3), "kazanma%": round(100 * float((r > 0).mean()), 1),
            "MDD_R": round(dd, 1), "giveback": round(give, 3) if give is not None else None,
            "giveback_medyan": round(give_med, 3) if give_med is not None else None,
            "+1R_görüp_zararla%": round(100 * float(((mfe >= 1) & (r < 0)).sum() / max(1, big.sum())), 1),
            "+1R_görüp_-0.3R_altı%": round(100 * float(((mfe >= 1) & (r <= -0.3)).sum() / max(1, big.sum())), 1),
            "+2R_görüp_zararla_adet": int(((mfe >= 2) & (r < 0)).sum()),
            "ort_kazanç_R": round(float(r[r > 0].mean()), 3), "ort_kayıp_R": round(float(r[r <= 0].mean()), 3),
            "funding_R_ort": round(float(np.mean([t.get("funding_r", 0) for t in tr])), 4)}


def port_metrics(tr, extra=None):
    ps = dict(PS, **(extra or {}))
    p = portfolio(tr, ps, 1000.0, returns_of=RETS.get if ps.get("filter_correlation") else None)
    st = p["stats"]
    kept = p["trades"]
    rr = np.array([t["r_multiple"] for t in kept]) if kept else np.zeros(0)
    return {"işlem": st["trades"], "getiri%": round(st["return_pct"], 1), "MDD%": round(st["max_drawdown_pct"], 1),
            "PF": round(st["profit_factor"], 3) if st["profit_factor"] else None,
            "beklenti_R": round(st["expectancy_r"], 4), "ort_işlem$": round(st["net_pnl"] / max(1, st["trades"]), 2),
            "net_R": round(float(rr.sum()), 1)}


RETS = {}


def load_rets():
    from backend.smcbot.risk import log_returns
    from backend.smcbot.smc import Candles
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import data
    for sym in data.SYMS:
        got = data.load(sym, Candles)
        if got:
            c4 = got[1]
            RETS[sym] = log_returns(c4.time + 14_400_000, c4.close)


def main():
    import variants
    out = {}
    if any(variants.V[v][0].get("filter_correlation") for v in sys.argv[2:]):
        load_rets()
    for v in sys.argv[2:]:
        tr = pickle.load(open(f"{v}.pkl", "rb"))
        extra = {k: x for k, x in variants.V[v][0].items() if k.startswith(("filter_correlation", "max_corr", "corr_"))}
        out[v] = {}
        for name, (a, b) in PER.items():
            seg = [t for t in tr if a <= t["entry_time"] < b]
            out[v][name] = {"ham": raw_metrics(seg), "portföy": port_metrics(seg, extra)}
    json.dump(out, open(sys.argv[2] + "_metrics.json" if len(sys.argv) == 3 else "metrics.json", "w"), ensure_ascii=False, indent=1)
    for name in PER:
        print("==", name)
        rows = []
        for v in out:
            h, p = out[v][name]["ham"], out[v][name]["portföy"]
            rows.append([v, h["işlem"], h["net_R"], h["beklenti_R"], h["PF"], h["kazanma%"], h["MDD_R"], h["giveback"],
                         h["+1R_görüp_-0.3R_altı%"], p["işlem"], p["getiri%"], p["MDD%"], p["PF"], p["ort_işlem$"]])
        print(pd.DataFrame(rows, columns=["var", "n", "netR", "expR", "PF", "win%", "MDD_R", "giveb", "1R->-.3R%",
                                          "P_n", "P_get%", "P_MDD%", "P_PF", "P_$/işl"]).to_string(index=False))


main()
