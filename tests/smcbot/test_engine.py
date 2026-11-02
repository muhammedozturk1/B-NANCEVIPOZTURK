"""Demo motoru ve risk kuralları: giriş -> TP1 (kısmi + başabaş stop) -> TP2 / stop akışı."""
import asyncio
from decimal import Decimal

import pytest

from backend.smcbot import db
from backend.smcbot.binance_client import SymbolInfo
from backend.smcbot.bot import Bot
from backend.smcbot.risk import RiskGuard, compute_stats, size_position

S = db.get_settings()
SYM = SymbolInfo("TESTUSDT", "TEST", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("0.001"),
                 Decimal("0.001"), Decimal("100000"), Decimal("5"))


def test_size_position_risk_and_cap():
    s = dict(S, risk_per_trade_pct=1.0, leverage=10, max_open_positions=3)
    z = size_position(1000, 100.0, 98.0, s, SYM)
    # 10 USDT risk / (2 + 100*(0.001+0.0002)) = ~4.67 adet
    assert z["risk_amount"] == pytest.approx(10, rel=0.01)
    assert not z["capped"]
    s5 = dict(s, risk_per_trade_pct=5.0)
    z2 = size_position(1000, 100.0, 99.5, s5, SYM)  # çok dar stop -> teminat sınırı devreye girer
    assert z2["capped"]
    assert z2["margin"] <= 1000 / 3 + 1e-6
    assert z2["risk_amount"] < 50


def _bot_with_trade(side="LONG"):
    bot = Bot()
    bot.md.symbols["TESTUSDT"] = SYM
    bot.save_settings({"running": True, "risk_per_trade_pct": 1.0, "mode": "paper"})
    bot.paper.reset()
    L = side == "LONG"
    entry, sl, tp1, tp2 = (100.0, 98.0, 102.0, 106.0) if L else (100.0, 102.0, 98.0, 94.0)
    tid = db.insert_trade({"mode": "paper", "symbol": "TESTUSDT", "side": side, "status": "pending", "score": 80,
                           "setup_time": bot.now(), "entry_price": entry, "sl": sl, "sl_initial": sl, "tp1": tp1,
                           "tp2": tp2, "leverage": 10, "reasons": [], "annotations": {},
                           "meta": {"key": f"k{side}{bot.now()}", "expiry_time": bot.now() + 10**9}})
    return bot, tid


def _mark(bot, price):
    bot.md.marks["TESTUSDT"] = (price, bot.now())


async def _run_path(bot, prices):
    for p in prices:
        _mark(bot, p)
        await bot._process_pending()
        await bot._process_open()


def test_long_tp1_then_tp2():
    bot, tid = _bot_with_trade("LONG")
    start = bot.paper.balance()
    asyncio.run(_run_path(bot, [100.5, 99.99, 101.0, 102.1, 104.0, 106.2]))
    t = db.get_trade(tid)
    assert t["status"] == "closed" and t["tp1_hit"] == 1
    assert t["exit_reason"].startswith("TP2")
    # ~ (0.5*1R + 0.5*3R) = 2R, eksi masraflar
    assert 1.6 < t["r_multiple"] < 2.1
    assert bot.paper.balance() == pytest.approx(start + t["pnl"], abs=1e-6)


def test_long_tp1_then_breakeven():
    bot, tid = _bot_with_trade("LONG")
    asyncio.run(_run_path(bot, [99.9, 102.2, 101.0, 99.5]))
    t = db.get_trade(tid)
    assert t["status"] == "closed"
    assert "Başabaş" in t["exit_reason"]
    assert t["pnl"] > 0  # TP1 kârı başabaş çıkışın masrafını karşılar


def test_short_stop_loss():
    bot, tid = _bot_with_trade("SHORT")
    asyncio.run(_run_path(bot, [100.1, 101.0, 102.05]))
    t = db.get_trade(tid)
    assert t["status"] == "closed" and t["exit_reason"] == "Stop"
    assert -1.15 < t["r_multiple"] < -0.95


def test_pending_cancel_when_runs_away():
    bot, tid = _bot_with_trade("LONG")
    asyncio.run(_run_path(bot, [100.5, 102.5]))
    assert db.get_trade(tid)["status"] == "cancelled"


def test_guard_limits():
    g = RiskGuard("paper")
    g.reset()
    s = dict(S)
    opens = [{"status": "open", "side": "LONG", "symbol": f"S{i}USDT"} for i in range(2)]
    ok, why = g.can_open("LONG", "XUSDT", opens, 0, s)
    assert not ok and "Aynı yönde" in why
    ok, _ = g.can_open("SHORT", "XUSDT", opens, 0, s)
    assert ok
    # drawdown kilidi
    g.update(1000, 1000, 0, s)
    msg = g.update(650, 650, 1, s)
    assert msg and g.state()["halted"]
    ok, why = g.can_open("SHORT", "XUSDT", [], 2, s)
    assert not ok and "Risk kilidi" in why
    g.unlock()
    assert not g.state()["halted"]


def test_stats():
    tr = [{"status": "closed", "pnl": p, "r_multiple": p / 10, "side": "LONG", "symbol": "A", "exit_time": i,
           "entry_time": i - 1, "exit_reason": "x", "fees": 0.1} for i, p in enumerate([10, -10, -10, 20, 5])]
    st = compute_stats(tr, 1000)
    assert st["trades"] == 5 and st["wins"] == 3
    assert st["profit_factor"] == pytest.approx(35 / 20)
    assert st["max_losing_streak"] == 2
