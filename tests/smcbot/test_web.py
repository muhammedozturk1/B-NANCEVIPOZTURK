"""Panel entegrasyonu: ortak giriş, CSRF, yönetim kilidi, paylaşılan testnet hesabında emir güvenliği."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.smcbot import config, db
from backend.smcbot.binance_client import BinanceError
from backend.smcbot.bot import LEASE, Bot
from backend.smcbot.brokers import LiveBroker
from tests.conftest import login
from tests.smcbot.test_engine import SYM


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "ENABLED", True)
    monkeypatch.setattr(config, "OFFLINE_SIM", True)
    from backend.dashboard.api import app
    with TestClient(app) as c:
        yield c


def test_panel_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/bot/start").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_state_csrf_settings_and_gate(client):
    login(client)
    st = client.get("/api/state").json()
    assert st["mode"] == "paper" and st["leader"] is True and st["running"] is False
    assert st["sim"] is True and "worker" in st

    # CSRF anahtarı olmadan durum değiştiren istek reddedilir
    assert client.post("/api/bot/start").status_code == 403
    tok = client.get("/api/session").json()["csrf"]
    h = {"X-CSRF-Token": tok}
    assert client.post("/api/bot/start", headers=h).json() == {"ok": True}
    assert client.get("/api/state").json()["running"] is True
    # başka siteden gelen istek (middleware)
    assert client.post("/api/bot/pause", headers={**h, "Origin": "https://evil.example"}).status_code == 403

    # ayarlar: mod buradan değişmez
    r = client.post("/api/settings", json={"risk_per_trade_pct": 1.5, "mode": "live"}, headers=h).json()
    assert r["settings"]["risk_per_trade_pct"] == 1.5 and r["settings"]["mode"] == "paper"

    # gerçek mod kilidi: demo geçmişi yok
    r = client.post("/api/mode", json={"mode": "live", "confirm": "GERÇEK"}, headers=h)
    assert r.status_code == 400 and "kilitli" in r.json()["detail"]
    # sıfırlama onay ister
    assert client.post("/api/reset", json={}, headers=h).status_code == 400
    assert client.post("/api/reset", json={"confirm": "SIFIRLA"}, headers=h).json() == {"ok": True}

    html = client.get("/").text
    assert "eqValue" in html and "/smc/" in html
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/smc/").status_code == 200  # SMC Scalper paneli yerinde


def test_lease_single_leader():
    assert db.lease_acquire("t", "a", 20_000)
    assert not db.lease_acquire("t", "b", 20_000)
    assert db.lease_acquire("t", "a", 20_000)  # yenileme
    db.lease_release("t", "a")
    assert db.lease_acquire("t", "b", 20_000)
    assert db.lease_acquire("x", "b", -1)       # süresi dolmuş kilit devralınabilir
    assert db.lease_acquire("x", "c", 20_000)


def test_two_instances_only_leader_trades(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_SIM", False)

    async def scenario():
        a, b = Bot(), Bot()
        await a._lease_tick()
        await b._lease_tick()
        assert a.is_leader and not b.is_leader
        with pytest.raises(ValueError):
            await b.set_running(True)
        await b._scan_tick()
        assert "Yedekte" in b.scan_info["status"]
        # eski kopya kapanırken kilidi bırakır, yenisi devralır ve ayarları veritabanından okur
        db.save_settings({"risk_per_trade_pct": 2.0})
        db.lease_release(LEASE, a.iid)
        a.is_leader = False
        await b._lease_tick()
        assert b.is_leader and b.settings["risk_per_trade_pct"] == 2.0
        await a.md.rest.close()
        await b.md.rest.close()

    asyncio.run(scenario())


class FakeRest:
    def __init__(self, positions):
        self.pos = positions
        self.cancelled: list[int] = []
        self.orders: list[tuple] = []
        self._aid = 100

    async def positions(self):
        return self.pos

    async def set_isolated(self, symbol):
        return None

    async def set_leverage(self, symbol, lev):
        return None

    async def market_order(self, symbol, side, qty, reduce_only=False, client_id=None):
        self.orders.append((symbol, side, qty, reduce_only))
        return {"avgPrice": "100", "executedQty": qty}

    async def algo_close_order(self, symbol, side, order_type, trigger_price, client_algo_id=None):
        self._aid += 1
        return {"algoId": self._aid}

    async def cancel_algo(self, algo_id):
        self.cancelled.append(algo_id)

    async def cancel_all_algo(self, symbol):
        raise AssertionError("Paylaşılan hesapta coin'in TÜM emirleri iptal edilmemeli")


def test_live_broker_touches_only_own_orders():
    s = dict(db.get_settings(), leverage=10)
    t = {"id": 7, "symbol": "TESTUSDT", "side": "LONG", "sl": 98.0, "tp2": 106.0, "entry_price": 100.0}

    async def scenario():
        # başka botun pozisyonu olan coinde işlem açılmaz
        other = FakeRest([{"symbol": "TESTUSDT", "positionAmt": "5"}])
        lb = LiveBroker("testnet", other, lambda: s, lambda *a: None)
        lb.hedge_mode = False
        with pytest.raises(BinanceError):
            await lb.open(t, 1.0, 100.0, SYM)
        assert other.orders == []

        rest = FakeRest([])
        lb = LiveBroker("testnet", rest, lambda: s, lambda *a: None)
        lb.hedge_mode = False
        fill = await lb.open(t, 1.0, 100.0, SYM)
        assert fill["meta"] == {"sl_algo": 101, "tp_algo": 102}
        rest.pos = [{"symbol": "TESTUSDT", "positionAmt": "1"}]
        res = await lb.reduce(dict(t, meta=fill["meta"]), 1.0, 101.0, "market", SYM)
        assert sorted(rest.cancelled) == [101, 102]
        assert rest.orders[-1] == ("TESTUSDT", "SELL", "1.000", True) and res["qty"] == 1.0

    asyncio.run(scenario())
