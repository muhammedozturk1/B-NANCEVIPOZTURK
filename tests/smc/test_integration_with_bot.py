"""SMC'nin mevcut botla aynı süreç, veritabanı ve testnet hesabını paylaşırken güvenli davrandığını doğrular."""
from sqlalchemy import inspect

from backend.smc.core.types import Side, utc_now
from backend.smc.database.db import Database
from backend.smc.execution.gateway import OrderInfo
from backend.smc.execution.kill_switch import flatten_all
from backend.smc.execution.reconciliation import reconcile
from backend.smc.execution.trade_manager import TradeManager
from backend.smc.risk.engine import Candidate, PortfolioState, TradingGate, evaluate_trade
from backend.smc.strategy.signal import TradeSetup
from tests.smc.conftest import make_settings, run
from tests.smc.fakes import FakeExchange


def _tm(repo, notifier, filters, scope="shared"):
    s = make_settings(account_scope=scope, entry_order_timeout_s=0.05)
    ex = FakeExchange()

    async def on_safe(reason, manual):
        pass
    return TradeManager(ex, repo, notifier, s, lambda _: filters, on_safe, poll_interval=0.01), ex, s


def _open(tm, s, filters):
    st = TradeSetup("TESTUSDT", Side.LONG, 100.0, 99.0, 101, 102, 103, 12, 0)
    d = evaluate_trade(Candidate(st, 0.0001, 1e6, {}), PortfolioState(100, 100, 100, [], 0, None, utc_now()),
                       TradingGate(True), filters, s)
    return run(tm.open_position(st, d, None))


def _foreign(ex, symbol="BTCUSDT"):
    """Mevcut botun (ccxt) açtığı pozisyon ve algo SL emri: 'sb' önekli değil."""
    ex.open_position(symbol, 0.5, 60000)
    ex.orders["x-foreign-sl"] = OrderInfo(symbol, "x-foreign-sl", "999", True, "SELL", "STOP_MARKET", "NEW", 0.5,
                                          trigger_price=59000)


def test_smc_tables_are_prefixed_and_do_not_clash_with_bot_tables():
    db = Database("sqlite:///:memory:")
    db.create_all()
    names = set(inspect(db.engine).get_table_names())
    assert names and all(n.startswith("smc_") for n in names)
    assert "trades" not in names and "bot_status" not in names


def test_shared_reconcile_ignores_other_bots_positions(repo, notifier, filters):
    tm, ex, s = _tm(repo, notifier, filters)
    _foreign(ex)
    rep = run(reconcile(ex, repo, tm))
    assert rep.clean and any("SMC'ye ait olmayan" in a for a in rep.actions)
    assert ex.orders["x-foreign-sl"].active  # yabancı emir iptal edilmedi


def test_exclusive_reconcile_flags_orphans(repo, notifier, filters):
    tm, ex, s = _tm(repo, notifier, filters, scope="exclusive")
    _foreign(ex)
    assert not run(reconcile(ex, repo, tm)).clean


def test_shared_kill_switch_closes_only_smc_positions(repo, notifier, filters):
    tm, ex, s = _tm(repo, notifier, filters)
    pos = _open(tm, s, filters)
    _foreign(ex)
    res = run(flatten_all(ex, repo, tm, "KILL_SWITCH", delay=0))
    assert res.ok
    assert "TESTUSDT" not in ex.positions_ and "BTCUSDT" in ex.positions_
    assert ex.orders["x-foreign-sl"].active
    assert not [o for o in ex.orders.values() if o.active and o.client_id.startswith("sb")]
    assert repo.get_position(pos.id).exit_reason == "KILL_SWITCH"


def test_shared_finalize_keeps_foreign_orders_on_same_symbol(repo, notifier, filters):
    tm, ex, s = _tm(repo, notifier, filters)
    pos = _open(tm, s, filters)
    ex.orders["x-foreign-tp"] = OrderInfo("TESTUSDT", "x-foreign-tp", "998", True, "SELL", "TAKE_PROFIT_MARKET",
                                          "NEW", 0.1, trigger_price=150)
    ex.trigger(repo.active_order(pos.id, "SL").client_id)
    run(tm.manage_all())
    assert repo.get_position(pos.id).state == "CLOSED"
    assert ex.orders["x-foreign-tp"].active


def test_worker_skips_symbols_with_foreign_positions(db):
    from backend.smc.worker import TradingWorker
    w = TradingWorker(make_settings(account_scope="shared"), db=db)
    w.gw = FakeExchange()
    w.tm.gw = w.gw
    w.notifier.notify = lambda *a: None
    _foreign(w.gw, "ETHUSDT")
    run(w.refresh_account())
    assert w.foreign_symbols == {"ETHUSDT"}


def test_runner_does_not_start_without_keys_or_when_disabled(monkeypatch):
    from backend.smc import runner
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "SMC_BINANCE_API_KEY", "SMC_BINANCE_API_SECRET"):
        monkeypatch.setenv(k, "")
    assert runner.start_in_thread() is None
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.setenv("SMC_ENABLED", "false")
    assert runner.start_in_thread() is None


def test_runner_rejects_mainnet_url_without_crashing(monkeypatch):
    from backend.smc import runner
    monkeypatch.setenv("SMC_BINANCE_REST_URL", "https://fapi.binance.com")
    assert runner.start_in_thread() is None


def test_main_start_smc_is_safe(monkeypatch):
    from backend import main
    from backend.smc import runner

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(runner, "start_in_thread", boom)
    assert main.start_smc_scalper() is None  # funding motoru etkilenmez


def test_trial_does_not_start_when_api_key_invalid(db):
    import httpx
    import pytest
    from backend.smc.database.models import TrialState
    from backend.smc.market_data.rest import BinanceAPIError, BinanceREST
    from backend.smc.worker import TradingWorker

    def handler(req):
        if req.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        return httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."})

    s = make_settings()
    w = TradingWorker(s, db=db)
    w.rest = BinanceREST(s.binance_rest_url, "bad", "bad", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    w.gw.rest = w.rest
    w.notifier.notify = lambda *a: None
    with pytest.raises(BinanceAPIError):
        run(w.bootstrap())
    with db.session() as sess:
        assert sess.get(TrialState, 1) is None  # süre başlamadı
