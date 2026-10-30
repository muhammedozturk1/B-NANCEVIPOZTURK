import time
from datetime import datetime, timedelta

from backend import config
from backend.db import repository
from backend.db import status as bot_status
from backend.db.database import get_session
from backend.db.models import FundingPosition
from backend.funding import engine as fe
from backend.funding import strategy as fs

H = fs.HOUR_MS


def _stats(**kw):
    base = {"n": 9, "expected_n": 9, "avg_apr": 0.30, "last_apr": 0.30, "min_rate": 0.0001,
            "interval_h": 8.0, "last_rates": [0.0002, 0.0002, 0.0002]}
    base.update(kw)
    return base


def test_reject_reason_matches_entry_ok():
    cases = [_stats(), _stats(avg_apr=0.05), _stats(last_apr=0.01), _stats(min_rate=-0.0001),
             _stats(n=2), _stats(n=0)]
    for st in cases:
        assert (fs.entry_reject_reason(st) is None) == fs.entry_ok(st)


def test_breakeven_days():
    # gidiş-dönüş %0.42 ; yıllık %15 -> ~10 gün (README'deki "3 gün yeter" iddiası yanlıştı)
    assert abs(fs.breakeven_days(0.15) - 0.0042 / 0.15 * 365) < 1e-9
    assert 10 < fs.breakeven_days(0.15) < 10.5
    assert fs.breakeven_days(0) == float("inf")


def test_exit_rules():
    assert fs.exit_reason(_stats(), 0.31, 1) == "fiyat_koruma"
    assert fs.exit_reason(_stats(avg_apr=-0.2), 0, 10) == "negatif_fonlama"
    assert fs.exit_reason(_stats(avg_apr=0.01), 0, 10) is None          # min tutma süresi
    assert fs.exit_reason(_stats(avg_apr=0.01), 0, 100) == "fonlama_dustu"
    assert fs.exit_reason(_stats(), 0, 100) is None


class FakeMarket:
    """8 saatte bir ödeme yapan sahte coinler. GOOD yüksek fonlama, LOW düşük, NEG negatif."""
    def __init__(self, now_ms):
        self.now = now_ms
        self.rate = {"GOOD/USDT": 0.0003, "LOW/USDT": 0.00002, "NEG/USDT": -0.0001}
        self.px = {s: 10.0 for s in self.rate}

    def candidates(self, limit=None):
        return list(self.rate)

    def current_rates(self):
        return dict(self.rate)

    def history(self, symbol, since_ms):
        start = self.now - 10 * 24 * H
        out, t = [], start - start % (8 * H)
        while t <= self.now:
            if t >= since_ms:
                out.append((t, self.rate[symbol], self.px[symbol]))
            t += 8 * H
        return out

    def prices(self, symbol):
        return self.px[symbol], self.px[symbol]


def _cycle(market, monkeypatch):
    monkeypatch.setattr(fe, "_market", market)
    monkeypatch.setattr(fe, "_now_ms", lambda: market.now)
    fe.run_cycle()


def test_engine_opens_only_good_and_reports(monkeypatch):
    now = int(time.time() * 1000)
    m = FakeMarket(now)
    _cycle(m, monkeypatch)

    s = get_session()
    pos = s.query(FundingPosition).all()
    s.close()
    assert [p.symbol for p in pos] == ["GOOD/USDT"]

    info = bot_status.read_all()["funding"]["info"]
    assert info["candidates"] == 3 and info["opened"] == ["GOOD/USDT"]
    top = {c["symbol"]: c for c in info["top"]}
    assert top["GOOD/USDT"]["ok"] and not top.get("LOW/USDT", {"ok": False})["ok"]
    assert bot_status.read_all()["worker"]["last_ok_at"] is not None


def test_engine_collects_funding_then_exits_on_negative(monkeypatch):
    now = int(time.time() * 1000)
    m = FakeMarket(now)
    _cycle(m, monkeypatch)
    bal0 = repository.get_virtual_balance()

    # 4 gün sonra: 12 ödeme toplanmalı
    m.now = now + 4 * 24 * H
    s = get_session()
    p = s.query(FundingPosition).first()
    p.opened_at = datetime.utcnow() - timedelta(days=4)
    s.commit(); s.close()
    _cycle(m, monkeypatch)
    s = get_session(); p = s.query(FundingPosition).first(); s.close()
    assert p.status == "open" and p.funding_payments >= 11
    assert abs(p.funding_usd - p.funding_payments * 0.0003 * p.amount * 10.0) < 1e-9

    # fonlama negatife dönsün -> son 3 günün ort. < %3 -> çıkış
    m.rate["GOOD/USDT"] = -0.0002
    m.now += 3 * 24 * H
    _cycle(m, monkeypatch)
    s = get_session(); p = s.query(FundingPosition).filter_by(symbol="GOOD/USDT").first(); s.close()
    assert p.status == "closed" and p.close_reason in ("fonlama_dustu", "negatif_fonlama")
    assert abs(repository.get_virtual_balance() - (bal0 + p.pnl_usd)) < 1e-9


def test_engine_error_is_recorded(monkeypatch):
    class Broken(FakeMarket):
        def current_rates(self):
            raise RuntimeError("binance erişilemiyor")
    from backend import main
    m = Broken(int(time.time() * 1000))
    monkeypatch.setattr(fe, "_market", m)
    main.run_funding_safely()   # exception dışarı taşmamalı
    st = bot_status.read_all()["funding"]
    assert "erişilemiyor" in st["last_error"]
