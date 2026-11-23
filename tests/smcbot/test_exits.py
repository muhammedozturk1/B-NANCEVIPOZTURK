"""Trend çıkış motoru (exits.py) ve entegrasyonu: LONG ve SHORT için
stop asla geri gitmez, kısmi TP tekrar etmez, kâr kilidi seviyeleri doğru, senkron ve rejim filtreleri,
borsa güvenliği (yönlü yuvarlama, çift stop, kurtarma, idempotent kısmi kapatma), geçmiş testte kötü sıra + funding."""
import asyncio
import random
from decimal import Decimal

import numpy as np
import pytest

from backend.smcbot import config, db, exits as X, trend
from backend.smcbot.binance_client import BinanceError
from backend.smcbot.bot import Bot
from backend.smcbot.brokers import LiveBroker
from backend.smcbot.risk import correlation_block, log_returns
from tests.smcbot.test_engine import SYM
from tests.smcbot.test_trend import H4, T0, candles, flat_then_breakout

S = dict(config.DEFAULT_SETTINGS)
USER = "1.0:0, 1.5:0.5, 2.0:1.0"
SIDES = [("LONG", 100.0, 90.0), ("SHORT", 100.0, 110.0)]


def cfg(**kw):
    base = dict(lock_tiers=X.parse_tiers(USER), lock_fee_cover=False, trail_mode="off")
    base.update(kw)
    return X.ExitConfig(**base)


# ------------------------------------------------------------------ ayar şeması
def test_parse_tiers_and_validation():
    assert X.parse_tiers("2.0:1.0, 1.0:0") == [(1.0, 0.0), (2.0, 1.0)]
    assert X.parse_tiers("") == [] and X.parse_tiers(None) == []
    assert X.parse_tiers("2:0.33;3:0.25", frac=True) == [(2.0, 0.33), (3.0, 0.25)]
    for bad, frac in (("1.0:1.5", False), ("abc", False), ("2:0.8, 3:0.5", True), ("-1:0", False)):
        with pytest.raises(ValueError):
            X.parse_tiers(bad, frac=frac)
    st = db.save_settings({"lock_tiers": "1:5", "exit_trigger_source": "xyz", "partial_tp_tiers": "2:0.33",
                           "trail_activate_r": 99})
    assert st["lock_tiers"] == config.DEFAULT_SETTINGS["lock_tiers"]  # hatalı kademe kaydedilmez
    assert st["exit_trigger_source"] == "candle_close" and st["partial_tp_tiers"] == "2:0.33"
    assert st["trail_activate_r"] == 10.0


# ------------------------------------------------------------------ 1R ve kâr kilidi
@pytest.mark.parametrize("side,entry,stop", SIDES)
def test_one_r_fixed_and_lock_levels(side, entry, stop):
    st = X.ExitState.new(side, entry, stop)
    assert st.one_r == 10.0
    d = st.d
    expected = {1.0: 0.0, 1.5: 0.5, 2.0: 1.0}
    for r, lk in expected.items():
        plan = X.on_trigger(st, cfg(), entry + d * r * 10)
        assert plan.stop == pytest.approx(entry + d * lk * 10)
        X.apply_stop(st, plan.stop, plan.stop_source)
        assert st.one_r == 10.0  # stop taşınsa da 1R değişmez
    # fiyat geri gelir: kilit düşmez, yeni öneri yok
    assert X.on_trigger(st, cfg(), entry).stop is None
    assert st.stop == pytest.approx(entry + d * 10) and st.lock_r == 1.0


@pytest.mark.parametrize("side,entry,stop", SIDES)
def test_lock_fee_cover_makes_breakeven_net_zero(side, entry, stop):
    st = X.ExitState.new(side, entry, stop)
    c = cfg(lock_fee_cover=True, cost_frac=0.0012)
    plan = X.on_trigger(st, c, entry + st.d * 10)
    assert plan.stop == pytest.approx(entry + st.d * entry * 0.0012)


@pytest.mark.parametrize("side,entry,stop", SIDES)
def test_stop_never_moves_backwards_random_paths(side, entry, stop):
    rng = random.Random(7)
    c = cfg(trail_mode="both", trail_activate_r=1.0, trail_atr_mult=2.0, trail_swing_n=3,
            partial_tiers=X.parse_tiers("2:0.3", frac=True))
    for _ in range(200):
        st = X.ExitState.new(side, entry, stop)
        p = entry
        lows, highs = [], []
        prev = st.stop
        for _ in range(60):
            p = max(1.0, p + rng.gauss(0, 3))
            lows.append(p - rng.random() * 3)
            highs.append(p + rng.random() * 3)
            plan = X.on_trigger(st, c, p, Decimal("0.01"))
            if plan.stop is not None:
                X.apply_stop(st, plan.stop, plan.stop_source)
            new, src = X.choose_stop(st, X.trail_candidates(st, c, p, 2.5, lows, highs), Decimal("0.01"))
            if new is not None:
                X.apply_stop(st, new, src)
            assert not X.more_protective(side, prev, st.stop)  # LONG: yeni >= eski, SHORT: yeni <= eski
            prev = st.stop
    with pytest.raises(ValueError):
        X.apply_stop(st, st.stop - st.d * 1.0, "geri")


@pytest.mark.parametrize("side,price,expected", [("LONG", 100.123, 100.13), ("SHORT", 100.127, 100.12)])
def test_stop_rounding_is_protective(side, price, expected):
    assert X.round_stop(side, price, Decimal("0.01")) == pytest.approx(expected)
    assert SYM.round_price(price, "up" if side == "LONG" else "down") == pytest.approx(expected)
    # yuvarlama sonrası iyileşme yoksa taşıma yok
    st = X.ExitState.new(side, 100.0, 90.0 if side == "LONG" else 110.0)
    st.stop = expected
    assert X.choose_stop(st, {"x": price}, Decimal("0.01")) == (None, "")


@pytest.mark.parametrize("side,entry,stop", SIDES)
def test_trailing_starts_only_after_activation(side, entry, stop):
    st = X.ExitState.new(side, entry, stop)
    c = cfg(lock_tiers=[], trail_mode="atr", trail_activate_r=1.5, trail_atr_mult=2.0)
    X.on_trigger(st, c, entry + st.d * 12)  # +1.2R
    assert X.trail_candidates(st, c, entry + st.d * 12, 2.0, [], []) == {}
    plan = X.on_trigger(st, c, entry + st.d * 15)  # +1.5R
    assert st.trail_active and plan.notes
    cand = X.trail_candidates(st, c, entry + st.d * 15, 2.0, [], [])
    assert list(cand.values()) == [pytest.approx(entry + st.d * 11)]


@pytest.mark.parametrize("side,entry,stop", SIDES)
def test_partial_tp_runs_once(side, entry, stop):
    st = X.ExitState.new(side, entry, stop)
    c = cfg(lock_tiers=[], partial_tiers=X.parse_tiers("1:0.5, 2:0.25", frac=True))
    assert X.on_trigger(st, c, entry + st.d * 11).partials == [(0, 0.5)]
    assert X.on_trigger(st, c, entry + st.d * 12).partials == [(0, 0.5)]  # emir gelmedi: hâlâ bekliyor
    st.partials_done.append(0)
    assert X.on_trigger(st, c, entry + st.d * 25).partials == [(1, 0.25)]
    st.partials_done.append(1)
    for px in (entry + st.d * 30, entry, entry + st.d * 30):
        assert X.on_trigger(st, c, px).partials == []


# ------------------------------------------------------------------ rejim filtreleri
def test_fng_missing_modes_and_thresholds():
    up = candles(flat_then_breakout(True))
    assert len(trend.evaluate("T", up, dict(S, fng_missing="allow"), None)[0]) == 1
    sigs, cands = trend.evaluate("T", up, dict(S, fng_missing="skip"), None)
    assert sigs == [] and "alınamadı" in cands[0]["status"]
    assert len(trend.evaluate("T", up, dict(S, fng_missing="neutral_fallback"), None)[0]) == 1
    # düşük F&G'de long açma (isteğe bağlı): neutral_fallback 50 sayar, 60 alt sınırında engellenir
    sigs, cands = trend.evaluate("T", up, dict(S, fng_missing="neutral_fallback", fng_long_min=60), None)
    assert sigs == [] and "< 60" in cands[0]["status"]
    assert trend.evaluate("T", up, dict(S, fng_long_max=90), 80)[0]  # eşik ayarlanabilir


def test_volume_body_close_strength_filters_independent():
    base = flat_then_breakout(True)
    weak_vol = np.full(len(base), 100.0)
    c = candles(base, vol=weak_vol)
    m = trend.bar_metrics(c, len(c) - 1)
    assert m["volume_ratio"] == pytest.approx(1.0)
    assert trend.evaluate("T", c, dict(S, filter_volume=False), 50)[0]
    sigs, cands = trend.evaluate("T", c, dict(S, filter_volume=True, min_volume_ratio=1.5), 50)
    assert sigs == [] and "Hacim" in cands[0]["status"]
    strong = weak_vol.copy()
    strong[-1] = 300
    assert trend.evaluate("T", candles(base, vol=strong), dict(S, filter_volume=True, min_volume_ratio=1.5), 50)[0]
    # gövde / kapanış gücü (spread 0.5: gövde ~3.7, aralık ~4.7)
    assert trend.evaluate("T", c, dict(S, filter_body=True, min_body_ratio=0.5), 50)[0]
    assert not trend.evaluate("T", c, dict(S, filter_body=True, min_body_ratio=0.95), 50)[0]
    assert trend.evaluate("T", c, dict(S, filter_close_strength=True, min_close_strength=0.8), 50)[0]
    assert not trend.evaluate("T", c, dict(S, filter_close_strength=True, min_close_strength=0.99), 50)[0]
    # SHORT'ta da aynalı ölçülür
    dn = candles(flat_then_breakout(False))
    assert trend.bar_metrics(dn.mirror(), len(dn) - 1)["close_strength"] > 0.8


def test_correlation_limit():
    rng = np.random.default_rng(1)
    t = T0 + np.arange(80) * H4
    a = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 80)))
    b = a * (1 + rng.normal(0, 0.001, 80))           # neredeyse aynı coin
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 80)))  # bağımsız
    rets = {"A": log_returns(t, a), "B": log_returns(t, b), "C": log_returns(t, c)}
    st = dict(S, filter_correlation=True, max_correlation=0.8, corr_lookback=60)
    open_ = [{"symbol": "A", "side": "LONG"}]
    assert "korelasyon" in correlation_block("B", "LONG", open_, rets.get, st)
    assert correlation_block("B", "SHORT", open_, rets.get, st) is None   # ters yön serbest
    assert correlation_block("C", "LONG", open_, rets.get, st) is None
    assert correlation_block("B", "LONG", open_, rets.get, dict(st, filter_correlation=False)) is None


# ------------------------------------------------------------------ geçmiş test doğruluğu
def _bars(rows, t0=T0, iv=H4):
    from backend.smcbot.smc import Candles
    a = np.asarray(rows, dtype=float)
    v = np.full(len(a), 100.0)
    return Candles(t0 + np.arange(len(a), dtype=np.int64) * iv, a[:, 0], a[:, 1], a[:, 2], a[:, 3], v, v / 2, iv)


def _breakout_series(after):
    """60 yatay mum + kırılım mumu + `after` (o,h,l,c) satırları."""
    rows = [(100, 100.5, 99.5, 100)] * 60 + [(100, 104.5, 99.9, 104)]
    return _bars(rows + list(after))


def test_simulate_same_bar_stop_wins_over_tp_and_lock():
    s = dict(S, fng_filter=False, slippage_pct=0.0, taker_fee_pct=0.0, lock_tiers="1.0:0",
             partial_tp_tiers="1.0:0.5", exit_trigger_source="mark_price", trail_mode="off")
    c = _breakout_series([(104, 104.2, 103.5, 104)])
    tr = trend.simulate("T", c, s, 0)
    assert tr and tr[0]["side"] == "LONG"
    one_r = tr[0]["entry_price"] - tr[0]["sl_initial"]
    e = tr[0]["entry_price"]
    # aynı mumda hem +2R tepe hem stop altı dip: kötü sıra -> stop, kısmi TP yok
    c = _breakout_series([(104, e + 2 * one_r, tr[0]["sl_initial"] - 1, 104)])
    t = trend.simulate("T", c, s, 0)[0]
    assert t["r_multiple"] == pytest.approx(-1.0) and t["partials"] == []
    # tepe +1R, dip kilidin (0R) altında ama ilk stopun üstünde: kilit aynı mumda tetiklenmiş sayılır
    c = _breakout_series([(104, e + 1.2 * one_r, e - 0.5 * one_r, 104)])
    t = trend.simulate("T", c, s, 0)[0]
    assert len(t["partials"]) == 1 and t["exit_reason"].startswith("Kâr kilidi")
    assert t["r_multiple"] == pytest.approx(0.5 * 1.0 + 0.5 * 0.0)


def test_simulate_gap_through_stop_exits_at_open_and_charges_funding():
    s = dict(S, fng_filter=False, slippage_pct=0.0, taker_fee_pct=0.0, lock_tiers="", trail_mode="off")
    base = trend.simulate("T", _breakout_series([(104, 104.2, 103.9, 104)] * 3), s, 0)[0]
    stop, e = base["sl_initial"], base["entry_price"]
    gap = trend.simulate("T", _breakout_series([(104, 104.2, 103.9, 104), (stop - 2, stop - 1, stop - 3, stop - 2)]), s, 0)[0]
    assert gap["exit_price"] == pytest.approx(stop - 2) and gap["r_multiple"] < -1
    # funding: pozitif oran long'a maliyet
    c = _breakout_series([(104, 104.2, 103.9, 104)] * 3)
    ft = int(c.time[61] + H4 + 3600_000)
    f = trend.simulate("T", c, s, 0, funding=([ft], [0.001]))[0]
    assert f["funding_r"] == pytest.approx(0.001 * 104 / (e - stop), rel=1e-6)
    assert f["r_multiple"] == pytest.approx(base["r_multiple"] - f["funding_r"])


def test_simulate_short_mirror_and_funding_sign():
    s = dict(S, fng_filter=False, slippage_pct=0.0, taker_fee_pct=0.0, lock_tiers="", trail_mode="off")
    rows = [(100, 100.5, 99.5, 100)] * 60 + [(100, 100.1, 95.5, 96)] + [(96, 96.1, 95.8, 96)] * 3
    c = _bars(rows)
    ft = int(c.time[61] + H4 + 3600_000)
    t = trend.simulate("T", c, s, 0, funding=([ft], [0.001]))[0]
    assert t["side"] == "SHORT" and t["funding_r"] < 0  # pozitif funding'i short alır


def test_new_engine_matches_old_rules_with_legacy_config():
    """Eski kurallar (kilit/kısmi yok, 4s swing trailing) yeni motorda aynı sonucu verir."""
    up = [100.0] * 40 + [100 + 2 * i for i in range(1, 40)] + [178 - 4 * i for i in range(1, 20)]
    s = dict(S, fng_filter=False)
    tr = trend.simulate("T", candles(up), s, 0, None, None, None, X.ExitConfig.legacy(s))
    longs = [t for t in tr if t["side"] == "LONG"]
    assert longs and longs[0]["r_multiple"] > 1 and longs[0]["exit_reason"] == "İz süren stop"


# ------------------------------------------------------------------ bot entegrasyonu (demo)
@pytest.mark.parametrize("up", [True, False])
def test_bot_partial_tp_once_and_state_sync(monkeypatch, up):
    monkeypatch.setattr(config, "OFFLINE_SIM", False)

    async def scenario():
        bot = Bot()
        bot.md.symbols["TESTUSDT"] = SYM
        bot.save_settings({"running": True, "risk_per_trade_pct": 1.0, "strategy": "trend", "lock_tiers": USER,
                           "partial_tp_tiers": "1.0:0.5", "exit_trigger_source": "mark_price"})
        bot.paper.reset()
        closes = flat_then_breakout(up)
        t0 = bot.now() - 60_000 - len(closes) * H4
        sig = trend.evaluate("TESTUSDT", candles(closes, t0=t0), bot.settings, 50)[0][0]
        await bot._register_signal(sig)
        bot.md.marks["TESTUSDT"] = (closes[-1], bot.now())
        await bot._process_pending()
        t = db.list_trades("paper", ("open",))[0]
        ex = t["meta"]["exit"]
        d = 1 if up else -1
        assert ex["one_r"] == pytest.approx(abs(t["entry_price"] - t["sl_initial"]))
        q0 = t["qty"]
        px = t["entry_price"] + d * 1.6 * ex["one_r"]
        bot.md.marks["TESTUSDT"] = (px, bot.now())
        await bot._process_open()
        t = db.get_trade(t["id"])
        assert t["qty_open"] == pytest.approx(SYM.round_qty(q0 - SYM.round_qty(q0 * 0.5)))
        assert t["meta"]["exit"]["partials_done"] == [0] and len(t["meta"]["partials"]) == 1
        lock = t["entry_price"] + d * 0.5 * ex["one_r"] + d * t["entry_price"] * 0.0012
        assert t["sl"] == pytest.approx(SYM.round_price(lock, "up" if up else "down"))
        assert t["pnl"] > 0
        bal = bot.paper.balance()
        for _ in range(3):  # aynı fiyat tekrar: ikinci kısmi kapatma yok
            await bot._process_open()
        t2 = db.get_trade(t["id"])
        assert t2["qty_open"] == t["qty_open"] and len(t2["meta"]["partials"]) == 1 and bot.paper.balance() == bal
        # fiyat geri döner: stop geri gitmez, kilit stopundan kapanır
        bot.md.marks["TESTUSDT"] = (t["entry_price"], bot.now())
        await bot._process_open()
        t3 = db.get_trade(t["id"])
        assert t3["status"] == "closed" and t3["exit_reason"] == "Kâr kilidi stopu" and t3["r_multiple"] > 0
        await bot.md.rest.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ borsa güvenliği (sahte REST)
class FakeRest:
    def __init__(self):
        self.algos: dict[int, dict] = {}
        self.next_id = 100
        self.cancel_fail: set[int] = set()
        self.orders: dict[str, dict] = {}
        self.pos = {"TESTUSDT": 1.0}
        self.calls = []

    async def algo_close_order(self, symbol, side, typ, trigger, cid=None):
        self.next_id += 1
        self.algos[self.next_id] = {"algoId": self.next_id, "symbol": symbol, "clientAlgoId": cid, "orderType": typ,
                                    "triggerPrice": trigger}
        self.calls.append(("place", trigger))
        return {"algoId": self.next_id}

    async def cancel_algo(self, aid):
        if aid in self.cancel_fail:
            raise BinanceError(500, -1000, "geçici hata")
        self.algos.pop(aid, None)

    async def open_algo_orders(self, symbol=None):
        return list(self.algos.values())

    async def positions(self):
        return [{"symbol": k, "positionAmt": str(v)} for k, v in self.pos.items() if v]

    async def market_order(self, symbol, side, qty, reduce_only=False, client_id=None):
        # Gerçek Binance gibi: kimlik yalnız AÇIK emirler arasında benzersiz; dolmuş emrin kimliği tekrar kabul edilir
        self.calls.append(("market", client_id, qty))
        o = {"avgPrice": "110", "executedQty": qty, "status": "FILLED", "updateTime": 1}
        self.orders[client_id] = o
        self.pos[symbol] = round(self.pos[symbol] - float(qty), 8)
        return o

    async def get_order(self, symbol, cid):
        if cid not in self.orders:
            raise BinanceError(400, -2013, "Order does not exist.")
        return self.orders[cid]


def _live():
    rest = FakeRest()
    lb = LiveBroker("testnet", rest, lambda: S, lambda lvl, msg: None)
    return lb, rest


@pytest.mark.parametrize("side,new,expected", [("LONG", 101.234, "101.24"), ("SHORT", 98.766, "98.76")])
def test_move_stop_keeps_new_id_when_cancel_fails_and_recover_cleans(side, new, expected):
    async def scenario():
        lb, rest = _live()
        old = (await rest.algo_close_order("TESTUSDT", "SELL", "STOP_MARKET", "90", "smc7sl"))["algoId"]
        rest.cancel_fail.add(old)
        t = {"id": 7, "symbol": "TESTUSDT", "side": side, "sl": new, "qty_open": 1.0, "meta": {"sl_algo": old}}
        meta = await lb.move_stop(t, new, SYM)
        assert rest.calls[-1] == ("place", expected)          # koruma yönünde yuvarlandı
        assert meta["sl_algo"] != old and meta["stale_algos"] == [old]
        assert len(rest.algos) == 2                              # geçici olarak çift stop
        rest.cancel_fail.clear()
        fixes = await lb.recover([dict(t, meta=meta)], {"TESTUSDT": SYM})
        assert list(rest.algos) == [meta["sl_algo"]]            # fazladan stop temizlendi
        assert fixes and fixes[0][1]["meta"]["stale_algos"] == []

    asyncio.run(scenario())


def test_recover_replaces_missing_stop_and_syncs_qty():
    async def scenario():
        lb, rest = _live()
        rest.pos["TESTUSDT"] = 0.6  # kısmi kapatma borsada dolmuş ama DB'ye yazılamamış
        t = {"id": 9, "symbol": "TESTUSDT", "side": "LONG", "sl": 95.0, "qty_open": 1.0, "meta": {"sl_algo": 555}}
        (tr, upd, note), = await lb.recover([t], {"TESTUSDT": SYM})
        assert upd["qty_open"] == 0.6 and "yeniden kondu" in note
        assert len(rest.algos) == 1 and upd["meta"]["sl_algo"] in rest.algos
        assert await lb.recover([dict(t, qty_open=0.6, meta=upd["meta"])], {"TESTUSDT": SYM}) == []  # artık tutarlı

    asyncio.run(scenario())


def test_partial_reduce_is_idempotent():
    async def scenario():
        lb, rest = _live()
        t = {"id": 3, "symbol": "TESTUSDT", "side": "LONG", "entry_price": 100.0, "meta": {}}
        r1 = await lb.reduce(t, 0.4, 110.0, "partial", SYM, client_id="smc3p0")
        r2 = await lb.reduce(t, 0.4, 110.0, "partial", SYM, client_id="smc3p0")  # tekrar deneme (ör. yanıt kayboldu)
        assert r1["qty"] == r2["qty"] == 0.4 and rest.pos["TESTUSDT"] == pytest.approx(0.6)
        assert sum(1 for c in rest.calls if c[0] == "market") == 1  # borsaya tek emir gitti
        await lb.reduce(t, 0.2, 110.0, "partial", SYM, client_id="smc3p1")  # farklı kademe normal gönderilir
        assert rest.pos["TESTUSDT"] == pytest.approx(0.4)

    asyncio.run(scenario())


def test_recover_matches_only_own_trade_ids_and_waits_for_pending_partial():
    async def scenario():
        lb, rest = _live()
        mine = (await rest.algo_close_order("TESTUSDT", "SELL", "STOP_MARKET", "95", "smc1s2"))["algoId"]
        other = (await rest.algo_close_order("TESTUSDT", "SELL", "STOP_MARKET", "94", "smc12s1"))["algoId"]
        scalper = (await rest.algo_close_order("TESTUSDT", "SELL", "STOP_MARKET", "93", "sb5sl123"))["algoId"]
        rest.pos["TESTUSDT"] = 0.6
        t = {"id": 1, "symbol": "TESTUSDT", "side": "LONG", "sl": 95.0, "qty_open": 1.0,
             "meta": {"sl_algo": mine, "exit": {"pending_partial": 0}}}
        assert await lb.recover([t], {"TESTUSDT": SYM}) == []  # miktar farkı: kısmi kapatma tamamlanınca yazılır
        assert set(rest.algos) == {mine, other, scalper}         # başka işlemin / botun stopuna dokunulmadı

    asyncio.run(scenario())
