"""4 saatlik trend takibi: sinyal, korku endeksi engeli, iz süren stop, güvenli kaldıraç, uçtan uca demo işlem."""
import asyncio

import numpy as np
import pytest

from backend.smcbot import config, db, trend
from backend.smcbot.backtest import portfolio
from backend.smcbot.bot import Bot
from backend.smcbot.risk import safe_leverage, size_position
from backend.smcbot.smc import Candles
from tests.smcbot.test_engine import SYM

H4 = 14_400_000
T0 = 1790812800000
S = dict(config.DEFAULT_SETTINGS)


def candles(closes, spread=0.5, vol=None, t0=T0):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + spread
    lo = np.minimum(o, c) - spread
    v = np.full(len(c), 100.0) if vol is None else np.asarray(vol, dtype=float)
    return Candles(t0 + np.arange(len(c), dtype=np.int64) * H4, o, h, lo, c, v, v / 2, H4)


def flat_then_breakout(up=True):
    base = [100 + 0.3 * np.sin(i / 2) for i in range(60)]
    return base + ([104.0] if up else [96.0])


def test_long_breakout_signal():
    c = candles(flat_then_breakout(True))
    sigs, _ = trend.evaluate("TESTUSDT", c, S, fng=50)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side == "LONG" and s.tp1 is None and s.tp2 is None
    assert s.entry == pytest.approx(104.0)
    assert s.stop < s.entry and s.annotations["strategy"] == "trend"
    assert s.annotations["breakout"]["price"] < 104.0
    assert "kırıp" in s.reasons[0]["text"]


def test_short_breakdown_signal_and_no_signal_inside_channel():
    sigs, _ = trend.evaluate("TESTUSDT", candles(flat_then_breakout(False)), S, fng=50)
    assert [x.side for x in sigs] == ["SHORT"] and sigs[0].stop > sigs[0].entry
    inside = [100 + 0.3 * np.sin(i / 2) for i in range(61)]
    assert trend.evaluate("TESTUSDT", candles(inside), S, fng=50)[0] == []


def test_fear_greed_blocks_extremes():
    sigs, cands = trend.evaluate("TESTUSDT", candles(flat_then_breakout(True)), S, fng=80)
    assert sigs == [] and "açgözlülük" in cands[0]["status"]
    sigs, cands = trend.evaluate("TESTUSDT", candles(flat_then_breakout(False)), S, fng=20)
    assert sigs == [] and "korku" in cands[0]["status"]
    # filtre kapalıysa veya endeks alınamadıysa engel yok
    assert len(trend.evaluate("TESTUSDT", candles(flat_then_breakout(True)), dict(S, fng_filter=False), fng=80)[0]) == 1
    assert len(trend.evaluate("TESTUSDT", candles(flat_then_breakout(True)), S, fng=None)[0]) == 1


def test_trailing_stop_moves_only_in_favour():
    c = candles([100 + i for i in range(30)])
    t = {"side": "LONG", "sl": 90.0}
    new = trend.new_trail_stop(t, c, 10)
    assert new == pytest.approx(float(c.low[-10:].min())) and new > 90
    assert trend.new_trail_stop({"side": "LONG", "sl": 500.0}, c, 10) is None
    ts = {"side": "SHORT", "sl": 200.0}
    assert trend.new_trail_stop(ts, c, 10) == pytest.approx(float(c.high[-10:].max()))


def test_safe_leverage_keeps_liquidation_beyond_stop():
    assert safe_leverage(0.04, 10) == 10
    assert safe_leverage(0.08, 10) == 7
    assert safe_leverage(0.14, 10) == 4
    for stop in (0.03, 0.08, 0.14, 0.25):
        lev = safe_leverage(stop, 10)
        assert 1 / lev - 0.01 > stop  # yaklaşık izole likidasyon mesafesi stoptan uzak
    z = size_position(1000, 100.0, 92.0, dict(S, risk_per_trade_pct=1.0), SYM)
    assert z["leverage"] == 7 and z["risk_amount"] == pytest.approx(10, rel=0.03)


def test_simulate_trend_and_portfolio_rules():
    up = [100.0] * 40 + [100 + 2 * i for i in range(1, 40)] + [178 - 4 * i for i in range(1, 20)]
    tr = trend.simulate("TESTUSDT", candles(up), dict(S, fng_filter=False), 0, {})
    longs = [t for t in tr if t["side"] == "LONG"]
    assert longs and longs[0]["r_multiple"] > 1 and longs[0]["exit_reason"] == "İz süren stop"
    # portföy: aynı coinde üst üste, aynı yönde 2'den fazla pozisyon açılmaz
    mk = lambda sym, side, a, b: {"symbol": sym, "side": side, "entry_time": a, "exit_time": b, "r_multiple": 1.0,  # noqa: E731
                                  "status": "closed"}
    p = portfolio([mk("A", "LONG", 0, 10), mk("A", "LONG", 5, 20), mk("B", "LONG", 1, 9), mk("C", "LONG", 2, 9),
                   mk("D", "SHORT", 3, 9)], dict(S, risk_per_trade_pct=1.0))
    assert [t["symbol"] for t in p["trades"]] == ["A", "B", "D"]


def test_bot_trend_trade_paper_end_to_end(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_SIM", False)

    async def scenario():
        bot = Bot()
        bot.md.symbols["TESTUSDT"] = SYM
        bot.save_settings({"running": True, "risk_per_trade_pct": 1.0, "strategy": "trend"})
        bot.paper.reset()
        closes = flat_then_breakout(True)
        t0 = bot.now() - 60_000 - len(closes) * H4  # son 4s mum 1 dk önce kapanmış olsun
        c = candles(closes, t0=t0)
        sig = trend.evaluate("TESTUSDT", c, bot.settings, 50)[0][0]
        await bot._register_signal(sig)
        t = db.list_trades("paper", ("pending",))[0]
        assert t["meta"]["exit_mode"] == "trail" and t["meta"]["market"]
        bot.md.marks["TESTUSDT"] = (104.1, bot.now())
        await bot._process_pending()
        t = db.get_trade(t["id"])
        assert t["status"] == "open" and t["tp2"] is None
        # fiyat yükselir, yeni 4s mumlar kapanır: stop iz sürer
        c2 = candles(closes + [106 + 2 * i for i in range(20)], t0=t0)
        await bot._trail_update(t, c2)
        t = db.get_trade(t["id"])
        assert t["sl"] > t["entry_price"] and len(t["meta"]["trail"]) == 1
        # hedefe değil, sadece stopa bakılır
        bot.md.marks["TESTUSDT"] = (200.0, bot.now())
        await bot._process_open()
        assert db.get_trade(t["id"])["status"] == "open"
        bot.md.marks["TESTUSDT"] = (t["sl"] - 0.5, bot.now())
        await bot._process_open()
        t = db.get_trade(t["id"])
        assert t["status"] == "closed" and t["exit_reason"] == "İz süren stop (kârda)" and t["pnl"] > 0
        await bot.md.rest.close()

    asyncio.run(scenario())


def test_stale_breakout_after_restart_is_not_registered(monkeypatch):
    """Bot yeniden başladığında son kapanmış 4s mum saatler önce kapanmış olabilir: geç girilmez, kayıt da açılmaz."""
    monkeypatch.setattr(config, "OFFLINE_SIM", False)
    from backend.smcbot import fng

    async def scenario():
        bot = Bot()
        bot.md.symbols["TESTUSDT"] = SYM
        bot.md.universe = ["TESTUSDT"]
        bot.save_settings({"running": True, "risk_per_trade_pct": 1.0, "strategy": "trend"})

        async def no_refresh(*a, **k):
            return None

        async def fake_fng(*a, **k):
            return 50

        closes = flat_then_breakout(True)
        state = {"age": 2 * 3_600_000}

        async def fake_candles(sym, iv, limit=300):
            return candles(closes, t0=bot.now() - state["age"] - len(closes) * H4)

        monkeypatch.setattr(bot.md, "refresh_universe", no_refresh)
        monkeypatch.setattr(bot.md, "candles", fake_candles)
        monkeypatch.setattr(fng, "latest", fake_fng)
        bot.md.marks["TESTUSDT"] = (104.1, bot.now())

        await bot.scan_trend()  # mum 2 saat önce kapanmış
        assert db.list_trades("paper", None) == []
        assert bot.candidates[0]["status"].startswith("Geç kalındı")

        state["age"] = 60_000  # yeni mum 1 dk önce kapanmış (normal tarama)
        closes.append(105.0)
        await bot.scan_trend()
        opened = db.list_trades("paper", ("open",))
        assert len(opened) == 1 and opened[0]["symbol"] == "TESTUSDT"
        await bot.md.rest.close()

    asyncio.run(scenario())
