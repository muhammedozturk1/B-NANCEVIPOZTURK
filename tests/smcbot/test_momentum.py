"""Momentum portföy stratejisi: skor, seçim, filtreler, risk paritesi + volatilite hedefi, tavanlar,
borsa minimumu, emir planı ve bot entegrasyonu (demo: açma/artırma/azaltma/kapatma, koruma stopu, risk limitleri)."""
import asyncio
import math
from decimal import Decimal

import numpy as np
import pytest

from backend.smcbot import config, db, momentum as M
from backend.smcbot.binance_client import SymbolInfo
from backend.smcbot.bot import Bot
from backend.smcbot.smc import Candles
from tests.smcbot.test_exits import FakeRest, _live

H = 3_600_000
CFG = M.MomentumConfig()
SYMS = CFG.universe


def sym_info(name: str, min_notional: float = 5.0) -> SymbolInfo:
    return SymbolInfo(name, name[:-4], Decimal("0.0001"), Decimal("0.001"), Decimal("0.001"), Decimal("0.001"),
                      Decimal("0.001"), Decimal("1000000"), Decimal(str(min_notional)))


def path(drift: float, n: int = 800, seed: int = 0, vol: float = 0.004, start: float = 100.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(drift + rng.normal(0, vol, n)))


def universe_paths(n: int = 800) -> dict[str, np.ndarray]:
    drifts = [0.0008, 0.0006, 0.0002, 0.0001, -0.0001, -0.0002, -0.0006, -0.0008]
    return {s: path(d, n, seed=i) for i, (s, d) in enumerate(zip(SYMS, drifts))}


# ------------------------------------------------------------------ saf fonksiyonlar
def test_score_sign_and_missing_data():
    assert M.score(path(0.001)) > 0
    assert M.score(path(-0.001)) < 0
    assert M.score(path(0.001, n=100)) is None          # 28 gün yok
    bad = path(0.001)
    bad[-5] = np.nan
    assert M.score(bad) is None


def test_select_top_bottom_quartile_and_filters():
    sc = {s: M.score(c) for s, c in universe_paths().items()}
    sel = M.select(sc, {}, {}, CFG)
    ranked = sorted(sc, key=sc.get, reverse=True)
    assert sel.longs == ranked[:2] and sel.shorts == ranked[-2:]
    # funding: long pahalı -> atlanır, yerine 3. coin GEÇMEZ (ortadakiler tutulmaz)
    sel = M.select(sc, {ranked[0]: 0.001, ranked[-1]: -0.001}, {}, CFG)
    assert sel.longs == [ranked[1]] and sel.shorts == [ranked[-2]]
    assert "funding" in sel.skipped[ranked[0]] and "funding" in sel.skipped[ranked[-1]]
    # hacim filtresi
    sel = M.select(sc, {}, {ranked[0]: 1e6}, CFG)
    assert ranked[0] not in sel.longs and "hacim" in sel.skipped[ranked[0]]
    # işaret şartı: hepsi pozitifse short yok
    up = {s: abs(v) + 0.1 for s, v in sc.items()}
    assert M.select(up, {}, {}, M.MomentumConfig(require_sign=True)).shorts == []
    assert len(M.select(up, {}, {}, M.MomentumConfig(require_sign=False)).shorts) == 2


def test_risk_parity_vol_target_and_caps():
    closes = {"A": path(0.0005, seed=1, vol=0.002), "B": path(-0.0005, seed=2, vol=0.008)}
    sel = M.Selection(["A"], ["B"], {"A": 1.0, "B": -1.0}, {})
    cfg = M.MomentumConfig(max_coin_pct=100, trade_risk_pct=100, max_leverage=10)
    t = M.target_weights(sel, closes, cfg)
    assert t.weights["A"] > 0 > t.weights["B"]
    # risk paritesi: az oynak coine daha çok ağırlık (vol oranı ~4x)
    assert abs(t.weights["A"]) / abs(t.weights["B"]) == pytest.approx(4.0, rel=0.25)
    # ölçekten sonra portföy volatilitesi hedefe eşit
    R = np.vstack([M.hourly_returns(closes[s], 720) for s in ("A", "B")])
    w = np.array([t.weights["A"], t.weights["B"]])
    pv = math.sqrt(w @ (np.cov(R) * M.HOURS_PER_YEAR) @ w)
    assert pv == pytest.approx(0.20, rel=1e-6)
    # tavanlar: coin başı, işlem riski, toplam kaldıraç
    t = M.target_weights(sel, closes, M.MomentumConfig(max_coin_pct=10, trade_risk_pct=100, max_leverage=10))
    assert max(abs(x) for x in t.weights.values()) <= 0.10 + 1e-12
    t = M.target_weights(sel, closes, M.MomentumConfig(max_coin_pct=100, trade_risk_pct=0.5, max_leverage=10))
    for s, wv in t.weights.items():
        assert abs(wv) * t.stop_pct[s] <= 0.005 + 1e-12      # stop olursa en fazla %0,5 kayıp
    t = M.target_weights(sel, closes, M.MomentumConfig(max_coin_pct=100, trade_risk_pct=100, max_leverage=0.3))
    assert sum(abs(x) for x in t.weights.values()) == pytest.approx(0.3)


def test_min_notional_skip_and_round_up():
    t = M.Targets({"BTCUSDT": 0.12, "SOLUSDT": -0.01}, {"BTCUSDT": 0.10, "SOLUSDT": 0.10}, 0.3, 1.0, [])
    notes = M.apply_min_notional(t, 200.0, {"BTCUSDT": 100.0, "SOLUSDT": 5.0}, M.MomentumConfig())
    assert t.weights == {}                                  # BTC 24$ < 100$, SOL 2$ < 5$
    assert any("BTCUSDT" in n and "açılmadı" in n for n in notes)
    t = M.Targets({"SOLUSDT": -0.02}, {"SOLUSDT": 0.10}, 0.3, 1.0, [])
    M.apply_min_notional(t, 200.0, {"SOLUSDT": 5.0}, M.MomentumConfig(min_notional_policy="round_up"))
    assert t.weights["SOLUSDT"] == pytest.approx(-5.25 / 200)  # %5 paylı minimuma yükseltildi, yön korundu


def test_plan_orders_order_band_and_flip():
    eq = 200.0
    mins = {s: 5.0 for s in ("A", "B", "C", "D", "E")}
    tgt = {"A": 0.10, "B": -0.10, "C": 0.11, "E": -0.05}
    cur = {"A": 20.5, "C": 10.0, "D": 15.0, "E": 12.0}
    orders = M.plan_orders(tgt, cur, eq, {}, mins, M.MomentumConfig(rebalance_band_pct=2.0))
    acts = [(o["symbol"], o["action"]) for o in orders]
    assert ("A", "add") not in acts and ("A", "reduce") not in acts    # 0,5$ fark: band altında
    assert ("D", "close") in acts and ("B", "open") in acts and ("C", "add") in acts
    assert acts.index(("E", "close")) < acts.index(("E", "open"))     # yön değişimi: önce kapat
    first_open = min(i for i, a in enumerate(acts) if a[1] in ("open", "add"))
    assert all(a[1] in ("close", "reduce") for a in acts[:first_open])  # önce azaltma/kapatma


# ------------------------------------------------------------------ bot entegrasyonu (demo)
def _setup_bot(monkeypatch, paths, **settings):
    monkeypatch.setattr(config, "OFFLINE_SIM", False)
    bot = Bot()
    for s in SYMS:
        bot.md.symbols[s] = sym_info(s, 100.0 if s == "BTCUSDT" else 20.0 if s == "ETHUSDT" else 5.0)
    bot.md.volumes = {s: 1e9 for s in SYMS}
    bot.md._last_volumes = 1e18
    bot.save_settings({"running": True, "strategy": "momentum", "paper_start_balance": 200.0, **settings})
    bot.paper.reset()
    now = bot.now()

    async def candles(sym, interval, limit=300):
        c = paths[sym][-limit:]
        n = len(c)
        t = (now // H - n) * H + np.arange(n) * H
        return Candles(t.astype(np.int64), c, c * 1.001, c * 0.999, c, np.ones(n), np.ones(n), H)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(bot.md, "candles", candles)
    monkeypatch.setattr(bot.md, "refresh_volumes", noop)
    sent = []
    monkeypatch.setattr(bot, "notify", lambda text: sent.append(text))
    for s in SYMS:
        bot.md.marks[s] = (float(paths[s][-1]), now)
    return bot, sent


def test_bot_rebalance_opens_portfolio_and_respects_min_notional(monkeypatch):
    paths = universe_paths()

    async def scenario():
        bot, sent = _setup_bot(monkeypatch, paths, mom_require_sign=False)
        snap = await bot.rebalance("test")
        opens = bot.mom_trades()
        assert {t["symbol"] for t in opens} == set(snap["targets"])
        assert snap["longs"] == ["BTCUSDT", "ETHUSDT"] and snap["shorts"] == ["LINKUSDT", "DOGEUSDT"]
        assert "BTCUSDT" not in snap["targets"]                       # 100$ minimum, 30$ tavan: açılamaz
        assert any("BTCUSDT" in n and "açılmadı" in n for n in snap["notes"])
        eq = 200.0
        for t in opens:
            assert t["notional"] <= 0.15 * eq * 1.01                    # coin tavanı
            assert abs(t["entry_price"] - t["sl"]) * t["qty"] <= 0.015 * eq * 1.01   # işlem riski tavanı
            assert (t["side"] == "LONG") == (t["sl"] < t["entry_price"])
        assert any("AÇILDI" in m and "Skor" in m for m in sent)        # Telegram: coin, yön, miktar, fiyat, skor
        # aynı dönemde ikinci tik tekrar rebalance yapmaz
        n0 = len(db.list_trades("paper", ("open", "closed", "cancelled"), limit=100))
        await bot._momentum_tick(bot.now())
        assert len(db.list_trades("paper", ("open", "closed", "cancelled"), limit=100)) == n0
        await bot.md.rest.close()

    asyncio.run(scenario())


def test_bot_rebalance_adjusts_closes_and_flips(monkeypatch):
    paths = universe_paths()

    async def scenario():
        bot, sent = _setup_bot(monkeypatch, paths, mom_require_sign=False)
        await bot.rebalance("ilk")
        before = {t["symbol"]: t for t in bot.mom_trades()}
        # momentum tersine döner: eski long'lar short tarafına geçer
        for s in SYMS:
            paths[s] = np.concatenate([paths[s], paths[s][-1] * np.exp(np.cumsum(
                -(paths[s][-1] / paths[s][-700] - 1) * 0.004 + np.zeros(700)))])
            bot.md.marks[s] = (float(paths[s][-1]), bot.now())
        snap = await bot.rebalance("ikinci")
        after = {t["symbol"]: t for t in bot.mom_trades()}
        assert set(after) == set(snap["targets"])
        for s, t in after.items():
            assert (t["side"] == "LONG") == (snap["targets"][s] > 0)
        closed = db.list_trades("paper", ("closed",), limit=50)
        assert {t["symbol"] for t in closed} >= (set(before) - set(after))
        assert all(t["exit_reason"].startswith("Rebalance") for t in closed)
        await bot.md.rest.close()

    asyncio.run(scenario())


def test_paper_protective_stop_and_max_drawdown_halt(monkeypatch):
    paths = universe_paths()

    async def scenario():
        bot, sent = _setup_bot(monkeypatch, paths, mom_require_sign=False)
        await bot.rebalance("test")
        t = bot.mom_trades()[0]
        bot.md.marks[t["symbol"]] = (t["sl"] * (0.99 if t["side"] == "LONG" else 1.01), bot.now())
        await bot._process_open()
        assert db.get_trade(t["id"])["exit_reason"] == "Koruma stopu"
        # zirve 200 → equity %10'dan fazla düşerse: hepsi kapanır, kilit, bot durur
        bal, eq, _ = await bot.equity()
        await bot._momentum_risk(bal, eq, bot.now())
        bot.paper._add(-0.11 * eq)
        bal, eq, _ = await bot.equity()
        await bot._momentum_risk(bal, eq, bot.now())
        assert bot.mom_trades() == []
        assert bot.guard.state()["halted"] and not bot.settings["running"]
        assert any("MAKSİMUM DRAWDOWN" in m for m in sent)
        await bot._momentum_tick(bot.now())                            # kilitliyken rebalance yok
        assert bot.mom_trades() == []
        bot.is_leader = True
        bot.unlock_risk()
        assert db.kv_get("mom:paper:peak") == 0.0                      # zirve şimdiki equity'den yeniden başlar
        await bot.md.rest.close()

    asyncio.run(scenario())


def test_daily_loss_limit_closes_and_pauses_until_next_day(monkeypatch):
    paths = universe_paths()

    async def scenario():
        bot, sent = _setup_bot(monkeypatch, paths, mom_require_sign=False, mom_max_dd_pct=50.0)
        await bot.rebalance("test")
        bal, eq, _ = await bot.equity()
        await bot._momentum_risk(bal, eq, bot.now())
        bot.paper._add(-0.04 * eq)
        bal, eq, _ = await bot.equity()
        await bot._momentum_risk(bal, eq, bot.now())
        assert bot.mom_trades() == [] and not bot.guard.state()["halted"]
        assert any("Günlük kayıp" in m for m in sent)
        db.kv_set("mom:paper:last_rebalance", 0)
        await bot._momentum_tick(bot.now())                            # aynı gün: yeni pozisyon yok
        assert bot.mom_trades() == []
        assert "Günlük kayıp" in bot.scan_info["status"]
        await bot.md.rest.close()

    asyncio.run(scenario())


def test_live_add_is_idempotent():
    async def scenario():
        lb, rest = _live()
        t = {"id": 5, "symbol": "TESTUSDT", "side": "LONG", "entry_price": 100.0, "meta": {}}
        from tests.smcbot.test_engine import SYM
        r1 = await lb.add(t, 0.3, 100.0, SYM, client_id="smc5a1")
        r2 = await lb.add(t, 0.3, 100.0, SYM, client_id="smc5a1")
        assert r1["qty"] == r2["qty"] == 0.3
        assert sum(1 for c in rest.calls if c[0] == "market") == 1

    assert isinstance(FakeRest(), FakeRest)
    asyncio.run(scenario())


def test_settings_validation():
    st = db.save_settings({"mom_universe": "btcusdt, ETHUSDT,,ETHUSDT", "mom_windows": "7,14,28", "mom_rebalance_hours": "12"})
    assert st["mom_universe"] == "BTCUSDT,ETHUSDT" and st["mom_rebalance_hours"] == 12
    st = db.save_settings({"mom_universe": "BTC; DROP TABLE", "mom_windows": "0,500", "mom_rebalance_hours": 5,
                           "strategy": "xyz", "mom_max_leverage": 9})
    assert st["mom_universe"] == config.DEFAULT_SETTINGS["mom_universe"]
    assert st["mom_windows"] == "7,14,28" and st["mom_rebalance_hours"] == 8
    assert st["strategy"] == config.DEFAULT_SETTINGS["strategy"] and st["mom_max_leverage"] == 2.0


def test_one_time_strategy_migration():
    db.kv_set("settings", {"strategy": "trend", "risk_per_trade_pct": 1.0})
    db.kv_set("migr:strategy_v45", None)
    db._migrate_strategy_v45()
    assert db.get_settings()["strategy"] == "momentum"
    db.save_settings({"strategy": "trend"})
    db._migrate_strategy_v45()                       # bir daha çalışmaz: kullanıcı seçimi korunur
    assert db.get_settings()["strategy"] == "trend"
