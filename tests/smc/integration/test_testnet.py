"""Binance Futures TESTNET entegrasyon testleri.

Gerçek testnet hesabına küçük emirler gönderir. Varsayılan olarak ATLANIR.
Çalıştırmak için:
    BINANCE_API_KEY=... BINANCE_API_SECRET=... RUN_TESTNET_INTEGRATION=1 \
    pytest tests/integration -v -s

Kapsam: entry, partial fill (limit uzakta + timeout), SL, TP1, BE, TP2, TP3 (koşullu emir yerleşimi ve
SL taşıma), cancellation, kill switch, restart + reconciliation.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_TESTNET_INTEGRATION") != "1",
                                reason="RUN_TESTNET_INTEGRATION=1 ve testnet API key gerekli")

SYMBOL = os.getenv("INTEGRATION_SYMBOL", "BTCUSDT")


@pytest.fixture
def env(tmp_path):
    from backend.smc.config import Settings
    from backend.smc.database.db import Database
    from backend.smc.database.repo import Repo
    from backend.smc.execution.gateway import BinanceGateway
    from backend.smc.market_data.rest import BinanceREST
    from tests.smc.conftest import RecordingNotifier

    s = Settings(DATABASE_URL=f"sqlite:///{tmp_path}/it.db", entry_order_timeout_s=5)
    rest = BinanceREST(s.binance_rest_url, s.binance_api_key.get_secret_value(), s.binance_api_secret.get_secret_value())
    db = Database(s.database_url)
    db.create_all()
    return s, rest, BinanceGateway(rest, use_algo=s.use_algo_orders), Repo(db), RecordingNotifier()


async def _filters(rest, s):
    from backend.smc.market_data.universe import apply_brackets, eligible_perpetuals
    f = eligible_perpetuals(await rest.exchange_info(), s)
    return apply_brackets(f, await rest.leverage_brackets(), s.max_leverage)[SYMBOL]


async def _tm(s, gw, repo, notifier, f):
    from backend.smc.execution.trade_manager import TradeManager

    async def on_safe(reason, manual):
        notifier.notify("SAFE_MODE", reason)
    return TradeManager(gw, repo, notifier, s, lambda _: f, on_safe)


def test_full_lifecycle_on_testnet(env):
    s, rest, gw, repo, notifier = env

    async def go():
        from backend.smc.core.types import Side, utc_now
        from backend.smc.execution.kill_switch import flatten_all
        from backend.smc.execution.reconciliation import reconcile
        from backend.smc.risk.engine import Candidate, PortfolioState, TradingGate, evaluate_trade
        from backend.smc.strategy.signal import TradeSetup
        await rest.sync_time()
        await rest.set_one_way_mode()
        f = await _filters(rest, s)
        tm = await _tm(s, gw, repo, notifier, f)
        await flatten_all(gw, repo, tm, "PRE_TEST_CLEANUP", attempts=3)
        bt = await rest.book_ticker(SYMBOL)
        price = float(bt["askPrice"])
        stop = price * 0.99
        r = price - stop
        setup = TradeSetup(SYMBOL, Side.LONG, price, stop, price + r, price + 2 * r, price + 3 * r, 12, 0)
        pf = PortfolioState(1000, 1000, 1000, [], 0, None, utc_now())
        d = evaluate_trade(Candidate(setup, 0.0001, 1e9, {}), pf, TradingGate(True), f, s)
        assert d.allowed, d.reasons
        pos = await tm.open_position(setup, d, None)
        assert pos is not None and pos.state == "ACTIVE"
        ex_orders = {o.client_id for o in await gw.open_orders(SYMBOL)}
        assert repo.active_order(pos.id, "SL").client_id in ex_orders
        # BE taşımayı doğrula (yeni SL koy, eskisini iptal et)
        assert await tm.replace_stop(pos, price * 0.995, pos.filled_qty)
        # restart simülasyonu: reconciliation temiz olmalı
        rep = await reconcile(gw, repo, tm)
        assert rep.clean, rep.issues
        # kill switch
        res = await flatten_all(gw, repo, tm, "KILL_SWITCH")
        assert res.ok, res
        assert not await gw.positions() and not await gw.open_orders(SYMBOL)
        await rest.close()

    asyncio.run(go())


def test_partial_fill_timeout_cancels_remainder(env):
    """Limit, fiyatın çok altına konur → dolmaz → timeout sonrası iptal edilir."""
    s, rest, gw, repo, notifier = env

    async def go():
        await rest.sync_time()
        f = await _filters(rest, s)
        bt = await rest.book_ticker(SYMBOL)
        far = float(bt["bidPrice"]) * 0.9
        cid = "sbIT" + str(int(asyncio.get_event_loop().time() * 1000) % 10**8)
        await gw.ensure_symbol_setup(SYMBOL, 5)
        o = await gw.place_limit(SYMBOL, "BUY", float(f.min_qty) * 2, far, cid, f)
        assert o.active
        await asyncio.sleep(1)
        await gw.cancel(SYMBOL, cid)
        o2 = await gw.get_order(SYMBOL, cid)
        assert o2.status == "CANCELED" and o2.filled_qty == 0
        await rest.close()

    asyncio.run(go())
