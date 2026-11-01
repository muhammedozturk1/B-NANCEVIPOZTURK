import asyncio
import hashlib
import hmac
import time
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.smc.api.auth import LoginRateLimiter, hash_password, verify_password
from backend.smc.api.main import create_app
from backend.smc.core.types import Candle
from backend.smc.market_data.candles import CandleStore
from backend.smc.market_data.rest import BinanceAPIError, BinanceREST, SlidingWindowLimiter
from backend.smc.market_data.universe import book_metrics, eligible_perpetuals
from backend.smc.risk.correlation import correlation, evaluate_correlation
from backend.smc.core.types import Side
from tests.smc.conftest import make_settings, run


# ------------------------------------------------------------------ config
def test_mainnet_trading_url_rejected():
    with pytest.raises(ValueError):
        make_settings(binance_rest_url="https://fapi.binance.com")
    with pytest.raises(ValueError):
        make_settings(max_leverage=20)
    s = make_settings(database_url="postgres://u:p@h/db")
    assert s.database_url.startswith("postgresql://")
    assert make_settings().public_dict()["binance_api_secret"] == "***"


# -------------------------------------------------------------------- REST
def _mock_rest(handler, **kw):
    return BinanceREST("https://testnet.binancefuture.com", "KEY", "SECRET",
                       client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


def test_signed_request_has_valid_signature():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("X-MBX-APIKEY")
        return httpx.Response(200, json={"ok": True})

    rest = _mock_rest(handler)
    run(rest.request("GET", "/fapi/v2/account", {"a": 1}, signed=True))
    q = urlparse(seen["url"]).query
    payload, sig = q.rsplit("&signature=", 1)
    assert hmac.new(b"SECRET", payload.encode(), hashlib.sha256).hexdigest() == sig
    assert "timestamp=" in payload and "recvWindow=" in payload and seen["key"] == "KEY"


def test_429_retries_with_backoff_and_5xx():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": -1003, "msg": "too many"})
        if calls["n"] == 2:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"serverTime": 1})

    rest = _mock_rest(handler)
    rest._backoff = lambda attempt: asyncio.sleep(0)
    assert run(rest.request("GET", "/fapi/v1/time")) == {"serverTime": 1}
    assert calls["n"] == 3


def test_timestamp_error_triggers_resync():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000) + 5000})
        if calls.count("/fapi/v2/account") == 1:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"})
        return httpx.Response(200, json={"ok": 1})

    rest = _mock_rest(handler)
    assert run(rest.request("GET", "/fapi/v2/account", signed=True)) == {"ok": 1}
    assert "/fapi/v1/time" in calls and rest.time.offset_ms > 4000


def test_4xx_not_retried_and_order_not_blindly_retried():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503, json={"code": -1007, "msg": "unknown"})

    rest = _mock_rest(handler)
    with pytest.raises(BinanceAPIError):
        run(rest.new_order(symbol="X", side="BUY", type="LIMIT"))
    assert calls["n"] == 1  # emir için kör tekrar yok (duplicate koruması gateway'de)


def test_sliding_window_limiter_blocks_when_full():
    async def go():
        lim = SlidingWindowLimiter(10, 0.2)
        await lim.acquire(10)
        t0 = time.monotonic()
        await lim.acquire(1)
        return time.monotonic() - t0
    assert run(go()) >= 0.15


def test_header_sync_increases_usage():
    lim = SlidingWindowLimiter(100, 60)
    lim.sync_from_header(80)
    assert lim.used == 80


# ---------------------------------------------------------------- gateway
def test_gateway_idempotent_returns_existing_order_after_timeout():
    from backend.smc.execution.gateway import BinanceGateway
    from decimal import Decimal
    from backend.smc.market_data.universe import SymbolFilters
    state = {"posted": 0}

    def handler(req):
        if req.method == "POST":
            state["posted"] += 1
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, json={"symbol": "X", "clientOrderId": "cid1", "orderId": 5, "status": "NEW",
                                         "side": "BUY", "type": "LIMIT", "origQty": "1", "executedQty": "0"})

    gw = BinanceGateway(_mock_rest(handler))
    f = SymbolFilters("X", Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("100"), Decimal("0.001"),
                      Decimal("100"), 5.0)
    o = run(gw.place_limit("X", "BUY", 1, 100, "cid1", f))
    assert o.client_id == "cid1" and state["posted"] == 1  # ikinci kez gönderilmedi


# ---------------------------------------------------------------- market
def test_candle_store_ignores_open_candles_and_detects_gaps():
    st = CandleStore()
    c = [Candle(i * 180_000, 1, 1, 1, 1, 1, (i + 1) * 180_000 - 1) for i in range(5)]
    st.set_history("X", "3m", c, server_now_ms=4 * 180_000 + 10)  # son mum açık
    assert len(st.get("X", "3m")) == 4
    st.add_closed("X", "3m", Candle(6 * 180_000, 1, 1, 1, 1, 1, 7 * 180_000 - 1))
    assert st.has_gap("X", "3m")


def test_book_metrics_and_universe_filter(settings):
    spread, depth = book_metrics({"bids": [["99.9", "10"], ["99", "100"]], "asks": [["100.1", "10"], ["101", "100"]]})
    assert spread == pytest.approx(0.002) and depth == pytest.approx(99.9 * 10 + 100.1 * 10)
    info = {"symbols": [
        {"symbol": "AUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING",
         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                     {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                     {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
        {"symbol": "BUSDT", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "status": "TRADING", "filters": []},
        {"symbol": "CBUSD", "contractType": "PERPETUAL", "quoteAsset": "BUSD", "status": "TRADING", "filters": []}]}
    assert list(eligible_perpetuals(info, settings)) == ["AUSDT"]


def test_correlation():
    import math
    a = [100 * math.exp(0.01 * math.sin(i)) for i in range(60)]
    b = [50 * math.exp(0.01 * math.sin(i)) for i in range(60)]
    c = [50 * math.exp(-0.01 * math.sin(i)) for i in range(60)]
    assert correlation(a, b) == pytest.approx(1.0)
    assert correlation(a, c) == pytest.approx(-1.0)
    s = make_settings()
    d = evaluate_correlation("X", Side.LONG, [("A", Side.LONG)], {"A": None}, s)
    assert d.risk_factor == s.correlated_risk_factor  # bilinmiyorsa muhafazakâr


# --------------------------------------------------------------- dashboard
def test_password_hash_and_rate_limit():
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h) and not verify_password("x", h)
    lim = LoginRateLimiter(2, 60)
    lim.fail("ip")
    lim.fail("ip")
    assert not lim.allowed("ip")


def test_dashboard_auth_health_and_kill_csrf(db):
    s = make_settings(dashboard_password_hash=hash_password("secretpassword"))
    client = TestClient(create_app(s, db))
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["db"] is True
    assert client.get("/smc/api/summary").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/login", data={"username": "admin", "password": "wrong"}).status_code == 401
    r = client.post("/login", data={"username": "admin", "password": "secretpassword"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/")
    assert page.status_code == 200 and "KILL SWITCH" in page.text
    summary = client.get("/smc/api/summary").json()
    assert summary["risk"]["risk_per_trade"] == 0.05 and summary["risk"]["max_open_risk"] == 0.15
    assert client.post("/smc/api/kill").status_code == 403  # CSRF yok
    import re
    csrf = re.search(r'const CSRF = "([^"]+)"', page.text).group(1)
    r = client.post("/smc/api/kill", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    from backend.smc.database.repo import Repo
    assert [c.command for c in Repo(db).pending_commands()] == ["KILL"]


def test_set_history_keeps_newer_ws_candles_and_flags_gaps():
    st = CandleStore()
    tf = 180_000
    mk = lambda i: Candle(i * tf, 1, 1, 1, 1, 1, (i + 1) * tf - 1)  # noqa: E731
    st.add_closed("X", "3m", mk(10))  # backfill sürerken WS'den gelen mum
    st.add_closed("X", "3m", mk(11))
    st.set_history("X", "3m", [mk(i) for i in range(0, 10)], server_now_ms=12 * tf)
    assert [c.open_time // tf for c in st.get("X", "3m")] == list(range(12))
    assert not st.has_gap("X", "3m")
    st2 = CandleStore()
    st2.add_closed("X", "3m", mk(20))
    st2.set_history("X", "3m", [mk(i) for i in range(0, 10)], server_now_ms=21 * tf)
    assert st2.has_gap("X", "3m")  # arada eksik mum var -> sinyal üretilmez, yeniden doldurulur


def test_active_profile_relaxes_thresholds_only():
    spec = make_settings(profile="spec")
    act = make_settings(profile="active")
    assert (spec.ote_low, spec.max_stop_pct, spec.trigger_break_bars) == (0.62, 0.03, 2)
    assert (act.ote_low, act.max_stop_pct, act.trigger_break_bars, act.confirm_lookback_3m) == (0.50, 0.04, 1, 12)
    # risk kuralları ve zorunlu puan eşiği değişmez
    for k in ("risk_per_trade", "max_total_open_risk", "daily_loss_limit", "max_leverage", "max_open_positions",
              "min_score", "min_rr", "liq_buffer_pct"):
        assert getattr(spec, k) == getattr(act, k), k
    # açıkça verilen değer profili ezer
    assert make_settings(profile="active", max_stop_pct=0.025).max_stop_pct == 0.025


def test_active_trigger_needs_one_bar_break():
    from backend.smc.backtest.engine import _pre_trigger
    tf = 180_000
    c = [Candle(0, 10, 11.5, 9.8, 10.4, 1, tf - 1, 0.5),          # 2 önceki mum: high 11.5
         Candle(tf, 10.4, 10.7, 10.3, 10.6, 1, 2 * tf - 1, 0.5),  # önceki mum:   high 10.7
         Candle(2 * tf, 10.6, 10.9, 10.5, 10.8, 1, 3 * tf - 1, 0.8)]  # yükseliş mumu, kapanış 10.8
    # son mumun high'ını (10.7) kırdı ama 2 öncekinin (11.5) kıramadı
    assert _pre_trigger(c, 1) and not _pre_trigger(c, 2)
