from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from backend import config
from backend.db.database import get_session
from backend.db.models import FundingPosition, Trade, TradeStatus


def test_startup_without_exchange_when_directional_engines_off(monkeypatch):
    """Testnet'e ulaşılamasa bile fonlama motoru çalışmaya devam etmeli."""
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
    assert "funding_engine" in ids and "position_monitor" not in ids

    # yönlü motor açık ama borsa yok -> çökme yok, motor işi eklenmez
    monkeypatch.setitem(config.ENGINE_ENABLED, "scalp", True)
    client = main.startup_checks()
    assert client is None
    assert "scalp_engine" not in {j.id for j in main.build_scheduler(client).get_jobs()}


def _seed():
    s = get_session()
    t = Trade(engine="scalp", symbol="XRP/USDT", side="buy", leverage=5, entry_price=1.4873,
              amount=100, risk_usd=1.0, stop_loss=1.47, take_profit_1=1.51, take_profit_2=1.51,
              status=TradeStatus.CLOSED_MANUAL, exit_price=1.4879, pnl_usd=-0.06,
              confluence_score=0, strategies_used="x",
              opened_at=datetime(2026, 9, 30, 21, 55), closed_at=datetime(2026, 9, 30, 23, 0))
    f = FundingPosition(symbol="GOOD/USDT", status="closed", amount=1, notional_usd=10, spot_entry=10,
                        perp_entry=10, last_funding_ts=0, pnl_usd=0.5,
                        opened_at=datetime(2026, 10, 1), closed_at=datetime(2026, 10, 8, 12))
    s.add_all([t, f]); s.commit(); s.close()


def test_api_trades_roi_dates_and_cumulative():
    from backend.dashboard.api import app
    _seed()
    with TestClient(app) as c:
        tr = c.get("/api/trades").json()[0]
        assert tr["opened_at"].endswith("Z")              # UTC işaretli -> tarayıcı kaydırmaz
        assert tr["roi_pct"] < 0                          # PnL eksi ise ROI de eksi (eski: +0.20%)
        assert tr["r_multiple"] == -0.06

        cum = c.get("/api/cumulative?period=daily").json()
        assert [x["period"] for x in cum] == ["2026-09-30", "2026-10-08"]
        assert cum[-1]["cumulative"] == 0.44              # fonlama K/Z'si de dahil
        wk = c.get("/api/cumulative?period=weekly").json()
        assert wk[0]["period"] == "2026-09-28"            # haftanın pazartesisi
        assert c.get("/api/cumulative?period=monthly").json()[1]["period"] == "2026-10-01"


def test_api_status_states():
    from backend.dashboard.api import app
    from backend.db import status as bot_status
    from backend.db.models import BotStatus
    with TestClient(app) as c:
        assert c.get("/api/status").json()["state"] == "unknown"
        bot_status.beat()
        assert c.get("/api/status").json()["state"] == "alive"
        s = get_session()
        s.query(BotStatus).filter_by(component="worker").first().last_ok_at = datetime.utcnow() - timedelta(hours=2)
        s.commit(); s.close()
        body = c.get("/api/status").json()
        assert body["state"] == "dead" and not body["alive"]
        bot_status.mark_error("funding", "x hata")
        assert c.get("/api/status").json()["components"]["funding"]["last_error"] == "x hata"


def test_dashboard_page_served():
    from backend.dashboard.api import app
    with TestClient(app) as c:
        html = c.get("/").text
        assert "statusPill" in html and "fundingScan" in html
