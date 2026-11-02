from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from backend import config
from backend.db.database import get_session
from tests.conftest import login


def test_startup_without_exchange_when_directional_engines_off(monkeypatch):
    """Testnet'e ulaşılamasa bile worker çökmemeli (SMC Scalper ve heartbeat çalışmaya devam eder)."""
    from backend import main
    import backend.exchange.binance_client as bc

    def boom(*a, **k):
        raise RuntimeError("testnet down")
    monkeypatch.setattr(bc, "BinanceClient", boom)
    monkeypatch.setattr(main.notifier, "send_heartbeat", lambda: None)
    monkeypatch.setitem(config.ENGINE_ENABLED, "scalp", False)
    monkeypatch.setitem(config.ENGINE_ENABLED, "day", False)
    monkeypatch.setitem(config.ENGINE_ENABLED, "swing", False)
    client = main.startup_checks()
    assert client is None
    sched = main.build_scheduler(client)
    ids = {j.id for j in sched.get_jobs()}
    assert "funding_engine" not in ids and "position_monitor" not in ids  # v4: funding kaldırıldı
    assert {"status_beat", "heartbeat"} <= ids

    # yönlü motor açık ama borsa yok -> çökme yok, motor işi eklenmez
    monkeypatch.setitem(config.ENGINE_ENABLED, "scalp", True)
    client = main.startup_checks()
    assert client is None
    assert "scalp_engine" not in {j.id for j in main.build_scheduler(client).get_jobs()}


def test_api_status_states():
    from backend.dashboard.api import app
    from backend.db import status as bot_status
    from backend.db.models import BotStatus
    with TestClient(app) as c:
        login(c)
        assert c.get("/api/status").json()["state"] == "unknown"
        bot_status.beat()
        assert c.get("/api/status").json()["state"] == "alive"
        s = get_session()
        s.query(BotStatus).filter_by(component="worker").first().last_ok_at = datetime.utcnow() - timedelta(hours=2)
        s.commit(); s.close()
        body = c.get("/api/status").json()
        assert body["state"] == "dead" and not body["alive"]
        bot_status.mark_error("smc", "x hata")
        assert c.get("/api/status").json()["components"]["smc"]["last_error"] == "x hata"


def test_dashboard_page_served():
    from backend.dashboard.api import app
    with TestClient(app) as c:
        login(c)
        html = c.get("/").text
        assert "statusPill" in html and "eqValue" in html and "fundingScan" not in html


def test_dashboard_requires_login_and_health_is_public():
    from backend.dashboard.api import app
    with TestClient(app) as c:
        assert c.get("/health").json()["db"] is True
        assert c.get("/api/trades").status_code == 401
        assert c.get("/api/state").status_code == 401
        assert c.get("/smc/api/summary").status_code == 401
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.post("/api/trade/1/close").status_code == 401
        bad = c.post("/login", data={"username": "admin", "password": "yanlis"})
        assert bad.status_code == 401
        login(c)
        # başka bir siteden gelen state değiştiren istek reddedilir
        assert c.post("/api/trade/1/close", headers={"Origin": "https://evil.example"}).status_code == 403
        smc = c.get("/smc/")
        assert smc.status_code == 200 and "KILL SWITCH" in smc.text
        assert c.get("/smc/api/summary").json()["risk"]["risk_per_trade"] == 0.05
        assert "/smc/" in c.get("/").text  # SMC Bot panelinde SMC Scalper linki
        assert c.get("/api/state").status_code == 503  # testlerde SMC Bot kapalı (SMCBOT_ENABLED=false)
