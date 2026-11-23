"""Kullanım: run.py REPO VARIANT OUT.pkl  — 20 coin için işlemleri üretir (portföy kuralı uygulanmadan)."""
import json, os, pickle, sys
from concurrent.futures import ProcessPoolExecutor
REPO, VAR, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, REPO)
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
import data  # noqa: E402
from backend.smcbot import config, trend  # noqa: E402
from backend.smcbot.smc import Candles  # noqa: E402
import variants  # noqa: E402


def one(sym):
    got = data.load(sym, Candles)
    if got is None:
        return []
    c1, c4, fund = got
    S, kw = variants.get(VAR, config.DEFAULT_SETTINGS)
    if VAR == "orig":
        return trend.simulate(sym, c4, S, 0, None)
    from backend.smcbot import exits as X
    if kw.get("use_defaults"):  # gönderilen config varsayılanları + varyantın açtığı filtreler (F&G hariç)
        S = dict(config.DEFAULT_SETTINGS, fng_filter=False, **variants.V[VAR][0])
    cfg = X.ExitConfig.legacy(S) if kw.get("legacy") else X.ExitConfig.from_settings(S)
    return trend.simulate(sym, c4, S, 0, None, c1 if kw.get("c1", True) else None,
                          fund if kw.get("funding", True) else None, cfg)


if __name__ == "__main__":
    with ProcessPoolExecutor(4) as ex:
        res = list(ex.map(one, data.SYMS))
    tr = [t for r in res for t in r]
    pickle.dump(tr, open(OUT, "wb"))
    print(VAR, len(tr))
