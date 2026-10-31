"""Worker'ın açılış (bootstrap) ve tarama yolunu sahte Binance REST yanıtlarıyla uçtan uca çalıştırır."""
import random

import httpx

from backend.smc.execution.state_machine import BotStatus
from backend.smc.market_data.rest import BinanceREST
from backend.smc.worker import TradingWorker
from tests.smc.conftest import make_settings, run

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _klines(path_params, now_ms):
    tf = {"3m": 180_000, "5m": 300_000, "15m": 900_000}[path_params["interval"]]
    limit = int(path_params.get("limit", 499))
    rng = random.Random(hash(path_params["symbol"] + path_params["interval"]) % 1000)
    start = now_ms - now_ms % tf - (limit - 1) * tf
    rows, p = [], 100.0
    for i in range(limit):
        o = p
        p *= 1 + rng.gauss(0, 0.003)
        v = rng.uniform(50, 150)
        t = start + i * tf
        rows.append([t, str(o), str(max(o, p) * 1.001), str(min(o, p) * 0.999), str(p), str(v), t + tf - 1,
                     "0", 10, str(v * 0.5), "0", "0"])
    return rows


def handler(req: httpx.Request) -> httpx.Response:
    import time
    now = int(time.time() * 1000)
    q = dict(req.url.params)
    p = req.url.path
    filt = [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]
    routes = {
        "/fapi/v1/time": lambda: {"serverTime": now},
        "/fapi/v1/exchangeInfo": lambda: {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT",
                                                       "status": "TRADING", "filters": filt} for s in SYMS]},
        "/fapi/v1/leverageBracket": lambda: [{"symbol": s, "brackets": [{"initialLeverage": 20, "maintMarginRatio": 0.004,
                                                                          "cum": 0}]} for s in SYMS],
        "/fapi/v1/positionSide/dual": lambda: {"dualSidePosition": False} if req.method == "GET" else {"code": 200},
        "/fapi/v1/ticker/24hr": lambda: [{"symbol": s, "quoteVolume": str(1e9 - i)} for i, s in enumerate(SYMS)],
        "/fapi/v1/depth": lambda: {"bids": [["99.99", "5000"]], "asks": [["100.01", "5000"]]},
        "/fapi/v1/klines": lambda: _klines(q, now),
        "/fapi/v2/account": lambda: {"totalMarginBalance": "5000", "totalWalletBalance": "5000",
                                     "availableBalance": "5000", "totalUnrealizedProfit": "0", "totalInitialMargin": "0"},
        "/fapi/v2/positionRisk": lambda: [],
        "/fapi/v1/openOrders": lambda: [],
        "/fapi/v1/openAlgoOrders": lambda: [],
    }
    if p not in routes:
        return httpx.Response(404, json={"code": -1, "msg": f"no route {p}"})
    return httpx.Response(200, json=routes[p]())


def test_bootstrap_universe_backfill_reconcile_and_scan(db):
    s = make_settings(min_24h_quote_volume=0, universe_size=3, fng_enabled=False)
    w = TradingWorker(s, db=db)
    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    w.rest = BinanceREST(s.binance_rest_url, "k", "s", client=mock)
    w.public = BinanceREST(s.public_data_rest_url, client=mock)
    w.gw.rest = w.rest
    w.notifier.notify = lambda kind, text: None

    async def go():
        await w.bootstrap()
        assert set(w.universe) == set(SYMS)
        await w.backfill(w.universe)
        assert len(w.candles.get("BTCUSDT", "3m")) >= 400
        assert w.status == BotStatus.RUNNING
        assert w.repo.state().day_start_equity == 100.0  # virtual equity
        last = w.candles.get("BTCUSDT", "3m")[-1].close_time
        await w.scan(last + 1)  # yapısal sinyaller DB'ye loglanır; ws hazır olmadığı için işlem açılmaz
        assert not w.repo.open_positions()
    run(go())
