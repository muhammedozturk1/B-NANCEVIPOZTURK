"""Momentum portföy stratejisinin bot içindeki çalıştırıcısı (Bot sınıfına karışım/mixin olarak eklenir).

Her coin pozisyonu bir işlem (trade) satırıdır: meta.strategy = "momentum", meta.exit_mode = "portfolio".
- Rebalance: her `mom_rebalance_hours` saatte bir (UTC 00:00'dan itibaren), kapanmış 1s mumlarla hedef ağırlıklar
  hesaplanır, önce azaltma/kapatma sonra açma/artırma emirleri verilir. Bot yeniden başlarsa aynı dönemde tekrar
  rebalance yapmaz (son rebalance zamanı veritabanında).
- Koruma stopu: her pozisyonda giriş ± k × günlük volatilite (borsada closePosition STOP_MARKET; demoda mark fiyatıyla).
  Stopla kapanan coin, bir sonraki rebalance'a kadar yeniden açılmaz (açma zaten sadece rebalance'ta yapılır).
- Risk: portföy zirveden `mom_max_dd_pct` düşerse tüm momentum pozisyonları kapatılır, bot kilitlenir (panelden
  "Kilidi kaldır" gerekir). Günlük kayıp `mom_daily_loss_pct`'e ulaşırsa pozisyonlar kapatılır ve o gün (UTC) yeni
  rebalance yapılmaz.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from . import db, telegram
from . import momentum as M
from .binance_client import BinanceError
from .strategy import fmt_price
from .brokers import LiveBroker

HOUR_MS = 3_600_000


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


class MomentumMixin:
    """bot.Bot içinde kullanılır; self.settings, self.md, self.broker, self.log, self.notify vb. Bot'tan gelir."""

    # ------------------------------------------------------------------ durum
    def _mk(self, name: str) -> str:
        return f"mom:{self.mode}:{name}"

    def is_momentum(self) -> bool:
        return self.settings.get("strategy") == "momentum"

    def mom_trades(self) -> list[dict]:
        return [t for t in self.open_trades() if (t.get("meta") or {}).get("strategy") == "momentum"]

    def mom_state(self) -> dict:
        return {
            "last_rebalance": db.kv_get(self._mk("last_rebalance"), 0),
            "last": db.kv_get(self._mk("last"), None),
            "peak": db.kv_get(self._mk("peak"), 0.0),
            "day": db.kv_get(self._mk("day"), None),
            "paused_day": db.kv_get(self._mk("paused_day"), None),
        }

    def mom_drawdown_pct(self, equity: float) -> float:
        peak = float(db.kv_get(self._mk("peak"), 0.0) or 0.0)
        return max(0.0, (peak - equity) / peak * 100) if peak > 0 else 0.0

    # ------------------------------------------------------------------ zamanlama
    async def _momentum_tick(self, now: int) -> None:
        """_scan_tick'ten çağrılır (bot çalışıyor ve lider iken)."""
        s = self.settings
        ims = int(s.get("mom_rebalance_hours", 8)) * HOUR_MS
        bucket_start = (now // ims) * ims
        self.scan_info["next_scan"] = bucket_start + ims + (0 if self.sim else 5000)
        # eski trend işlemleri açık kaldıysa iz süren stoplarını yönetmeye devam et (4s kapanışlarında)
        b4 = now // (4 * HOUR_MS)
        if b4 != getattr(self, "_mom_trend_bucket", None):
            self._mom_trend_bucket = b4
            await self._manage_leftover_trend()
        if self.guard.state()["halted"]:
            self.scan_info["status"] = "Risk kilidi: rebalance yapılmıyor"
            return
        if db.kv_get(self._mk("paused_day")) == _utc_day(now):
            self.scan_info["status"] = "Günlük kayıp limiti: yarın (UTC) devam"
            return
        last = int(db.kv_get(self._mk("last_rebalance"), 0) or 0)
        if last >= bucket_start:
            self.scan_info["status"] = "Bekliyor (sonraki rebalance)"
            return
        if not self.sim and now - bucket_start < 5000:
            return  # mum kapanışından sonra birkaç sn bekle
        await self.rebalance("planlı")

    async def _manage_leftover_trend(self) -> None:
        trend = [t for t in self.open_trades() if (t.get("meta") or {}).get("exit_mode") == "trail"]
        if not trend:
            return
        async with self.lock:
            for t in trend:
                c4 = await self.md.candles(t["symbol"], "4h", 120)
                if c4 is not None:
                    await self._manage_bar(t, c4)

    # ------------------------------------------------------------------ rebalance
    async def rebalance(self, reason: str = "planlı") -> dict:
        s = self.settings
        cfg = M.MomentumConfig.from_settings(s)
        t0 = time.time()
        self.scan_info["status"] = "Rebalance hesaplanıyor…"
        if not self.md.symbols:
            await self.md.refresh_universe(int(s.get("universe_size", 30)), force=True)
        await self.md.refresh_volumes()
        now = self.now()
        other = {t["symbol"] for t in self.open_trades() if (t.get("meta") or {}).get("strategy") != "momentum"}
        skipped: dict[str, str] = {}
        closes: dict = {}
        for sym in cfg.universe:
            if sym not in self.md.symbols:
                skipped[sym] = "Binance'te bulunamadı"
                continue
            if sym in other:
                skipped[sym] = "bu coinde başka stratejinin açık işlemi var"
                continue
            c = await self.md.candles(sym, "1h", cfg.history_hours + 24)
            if c is None or len(c.close) < cfg.history_hours:
                skipped[sym] = "1s mum geçmişi alınamadı"
                continue
            closes[sym] = c.close
        scores = {sym: M.score(c, cfg.windows_days) for sym, c in closes.items()}
        funding = {sym: (self.md.funding.get(sym) or (None, 0))[0] for sym in closes}
        volume = {sym: self.md.volumes.get(sym) for sym in closes}
        sel = M.select(scores, funding, volume, cfg)
        skipped.update(sel.skipped)
        tg = M.target_weights(sel, closes, cfg)
        bal, eq, _ = await self.equity()
        mins = {sym: float(self.md.symbols[sym].min_notional or 5.0) for sym in closes}
        notes = tg.notes + M.apply_min_notional(tg, eq, mins, cfg)
        opens = {t["symbol"]: t for t in self.mom_trades()}
        current = {}
        prices = {}
        for sym in set(closes) | set(opens):
            mark = self.md.mark(sym) or (float(closes[sym][-1]) if sym in closes else None)
            if mark:
                prices[sym] = mark
        for sym, t in opens.items():
            if sym in prices:
                current[sym] = (1 if t["side"] == "LONG" else -1) * (t["qty_open"] or 0) * prices[sym]
        orders = M.plan_orders(tg.weights, current, eq, prices, mins, cfg)
        done, errors = [], []
        async with self.lock:
            for o in orders:
                try:
                    msg = await self._mom_execute(o, opens.get(o["symbol"]), prices, tg, sel, eq)
                    if msg:
                        done.append(msg)
                except BinanceError as e:
                    errors.append(f"{o['symbol']}: {e.msg}")
                    self.log("error", f"Rebalance emri başarısız ({o['symbol']} {o['action']}): {e}")
                except Exception as e:  # noqa: BLE001  tek coindeki hata tüm rebalance'ı durdurmasın
                    errors.append(f"{o['symbol']}: {e}")
                    self.log("error", f"Rebalance hatası ({o['symbol']} {o['action']}): {e!r}")
        db.kv_set(self._mk("last_rebalance"), now)
        gross = sum(abs(w) for w in tg.weights.values())
        snap = {
            "time": now, "reason": reason, "equity": eq, "scores": {k: (round(v, 3) if v is not None else None)
                                                                    for k, v in scores.items()},
            "longs": sel.longs, "shorts": sel.shorts, "targets": {k: round(v, 4) for k, v in tg.weights.items()},
            "stop_pct": {k: round(v, 4) for k, v in tg.stop_pct.items()}, "port_vol": round(tg.port_vol, 4),
            "scale": round(tg.scale, 3), "gross": round(gross, 3), "skipped": skipped, "notes": notes,
            "orders": done, "errors": errors,
        }
        db.kv_set(self._mk("last"), snap)
        self._mom_candidates(snap)
        dt = time.time() - t0
        summary = (f"Rebalance ({reason}): long {', '.join(sel.longs) or '-'} · short {', '.join(sel.shorts) or '-'} · "
                   f"toplam nominal %{gross * 100:.0f} · {len(done)} emir" + (f" · {len(errors)} hata" if errors else ""))
        self.scan_info.update(last_scan=now, duration=round(dt, 2), scanned=len(closes), signals=len(tg.weights),
                              status="Bekliyor (sonraki rebalance)", summary=summary)
        self.log("info", summary)
        for n in notes:
            self.log("info", "Rebalance notu: " + n)
        if errors:
            self.notify(telegram.msg_alert("Rebalance emir hatası", "; ".join(errors)[:900]))
        return snap

    async def _mom_execute(self, o: dict, t: dict | None, prices: dict, tg: M.Targets, sel: M.Selection,
                           eq: float) -> str | None:
        sym_name = o["symbol"]
        sym = self.md.symbols.get(sym_name)
        price = prices.get(sym_name)
        if sym is None or not price:
            return None
        score = sel.scores.get(sym_name)
        if o["action"] == "close":
            if t is None:
                return None
            t = db.get_trade(t["id"]) or t
            if t["status"] != "open":
                return None
            await self._close(t, price, "market", "Rebalance: hedef portföyde yok" if o["target"] == 0
                              else "Rebalance: yön değişti")
            return f"{sym_name} kapatıldı"
        side = "LONG" if o["target"] > 0 else "SHORT"   # açma/artırmada hedefin işareti
        if o["action"] == "open":
            qty = sym.round_qty(abs(o["delta"]) / price)
            if not sym.min_qty_ok(qty, price):
                self.log("info", f"{sym_name}: {abs(o['delta']):.2f}$ borsa minimumunun altında, açılmadı")
                return None
            return await self._mom_open(sym_name, side, qty, price, tg.stop_pct.get(sym_name, 0.12), score, o)
        if t is None:
            return None
        if o["action"] == "add":
            qty = sym.round_qty(abs(o["delta"]) / price)
            if not sym.min_qty_ok(qty, price):
                return None
            return await self._mom_add(t, qty, price, tg.stop_pct.get(sym_name, 0.12), score)
        # reduce
        qty = sym.round_qty(abs(o["delta"]) / price)
        rest = sym.round_qty((t["qty_open"] or 0) - qty)
        if rest <= 0 or not sym.min_qty_ok(rest, price):
            await self._close(t, price, "market", "Rebalance: kalan miktar borsa minimumunun altında")
            return f"{sym_name} kapatıldı (kalan küçük)"
        if not sym.min_qty_ok(qty, price):
            return None
        return await self._mom_reduce(t, qty, price, score)

    async def _mom_open(self, symbol: str, side: str, qty: float, price: float, stop_pct: float,
                        score: float | None, o: dict) -> str:
        s = self.settings
        d = 1 if side == "LONG" else -1
        sl = price * (1 - d * stop_pct)
        lev = max(1, int(math.ceil(float(s.get("mom_max_leverage", 1.5)))))
        tid = db.insert_trade({
            "mode": self.mode, "symbol": symbol, "side": side, "status": "pending", "score": score,
            "setup_time": self.now(), "entry_price": price, "sl": sl, "sl_initial": sl, "tp1": None, "tp2": None,
            "leverage": lev, "reasons": [{"ok": True, "pts": 0, "text": f"Momentum skoru {score:+.2f}" if score is not None
                                          else "Momentum"}],
            "annotations": {"strategy": "momentum", "tf": "1h"},
            "meta": {"strategy": "momentum", "exit_mode": "portfolio", "stop_pct": stop_pct, "target": o["target"]}})
        t = db.get_trade(tid)
        sym = self.md.symbols.get(symbol)
        try:
            fill = await self.broker.open(t, qty, price, sym)
        except BinanceError as e:
            self._cancel(t, f"Emir hatası: {e.msg}")
            raise
        entry, q = fill["price"], fill["qty"]
        sl = entry * (1 - d * stop_pct)
        meta = dict(t.get("meta") or {})
        meta.update(fill.get("meta") or {})
        if abs(sl - t["sl"]) / entry > 1e-4 and isinstance(self.broker, LiveBroker):
            try:  # stop gerçek giriş fiyatına göre yeniden konur
                meta.update(await self.broker.move_stop(dict(t, meta=meta), sl, sym))
            except BinanceError as e:
                self.log("warn", f"#{tid} stop giriş fiyatına göre güncellenemedi: {e} (ilk stop yerinde)")
                sl = t["sl"]
        upd = dict(status="open", entry_time=self.now(), entry_price=entry, qty=q, qty_open=q, notional=entry * q,
                   margin=entry * q / lev, risk_amount=abs(entry - sl) * q, fees=fill["fee"], pnl=-fill["fee"],
                   sl=sl, sl_initial=sl, meta=meta)
        db.update_trade(tid, **upd)
        t.update(upd)
        self.log("info", f"#{tid} {symbol} {side} AÇILDI (momentum) @ {fmt_price(entry)}, miktar {q:g} "
                         f"({entry * q:.2f}$), skor {score:+.2f}, koruma stopu {fmt_price(sl)}")
        self.notify(telegram.msg_momentum("AÇILDI", t, q, entry, score, self.mode))
        return f"{symbol} {side} açıldı"

    async def _mom_add(self, t: dict, qty: float, price: float, stop_pct: float, score: float | None) -> str:
        sym = self.md.symbols.get(t["symbol"])
        d = 1 if t["side"] == "LONG" else -1
        n = int((t.get("meta") or {}).get("adds", 0)) + 1
        fill = await self.broker.add(t, qty, price, sym, client_id=f"smc{t['id']}a{n}")
        q0, q = float(t["qty_open"] or 0), float(fill["qty"])
        entry = (t["entry_price"] * q0 + fill["price"] * q) / (q0 + q)
        sl = entry * (1 - d * stop_pct)
        meta = dict(t.get("meta") or {})
        meta["adds"] = n
        meta.setdefault("fills", []).append({"time": self.now(), "price": fill["price"], "qty": q, "kind": "add"})
        if isinstance(self.broker, LiveBroker):
            try:
                meta.update(await self.broker.move_stop(dict(t, meta=meta), sl, sym))
            except BinanceError as e:
                self.log("warn", f"#{t['id']} artırma sonrası stop güncellenemedi: {e} (önceki stop yerinde)")
                sl = t["sl"]
        upd = dict(entry_price=entry, qty=(t["qty"] or 0) + q, qty_open=q0 + q, notional=entry * (q0 + q),
                   risk_amount=max(float(t["risk_amount"] or 0), abs(entry - sl) * (q0 + q)), fees=(t["fees"] or 0) + fill["fee"],
                   pnl=(t["pnl"] or 0) - fill["fee"], sl=sl, meta=meta, score=score)
        db.update_trade(t["id"], **upd)
        t.update(upd)
        self.log("info", f"#{t['id']} {t['symbol']} {t['side']} ARTIRILDI +{q:g} @ {fmt_price(fill['price'])} "
                         f"(toplam {q0 + q:g}), stop {fmt_price(sl)}")
        self.notify(telegram.msg_momentum("ARTIRILDI", t, q, fill["price"], score, self.mode))
        return f"{t['symbol']} artırıldı"

    async def _mom_reduce(self, t: dict, qty: float, price: float, score: float | None) -> str:
        sym = self.md.symbols.get(t["symbol"])
        n = len((t.get("meta") or {}).get("partials", [])) + 1
        res = await self.broker.reduce(t, qty, price, "partial", sym, client_id=f"smc{t['id']}m{n}")
        q = float(res.get("qty") or qty)
        meta = dict(t.get("meta") or {})
        meta.setdefault("partials", []).append({"time": self.now(), "price": res["price"], "qty": q, "tier": f"reb{n}"})
        meta["last_reduce_time"] = max(self.now(), int(res.get("time") or 0))
        rest = sym.round_qty((t["qty_open"] or 0) - q) if sym else (t["qty_open"] or 0) - q
        add = res["pnl"] - res["fee"]
        # risk_amount (R hesabının paydası) pozisyonun en büyük olduğu andaki stop riskidir: azaltmada değişmez
        upd = dict(qty_open=rest, pnl=(t["pnl"] or 0) + add, fees=(t["fees"] or 0) + res["fee"], meta=meta)
        db.update_trade(t["id"], **upd)
        t.update(upd)
        self.log("info", f"#{t['id']} {t['symbol']} {t['side']} AZALTILDI -{q:g} @ {fmt_price(res['price'])} "
                         f"({add:+.2f} USDT), kalan {rest:g}")
        self.notify(telegram.msg_momentum("AZALTILDI", t, q, res["price"], score, self.mode, pnl=add))
        return f"{t['symbol']} azaltıldı"

    # ------------------------------------------------------------------ risk
    async def _momentum_risk(self, bal: float, eq: float, now: int) -> None:
        """_slow_tick'ten çağrılır. Zirveden düşüş ve günlük kayıp kontrolü (yalnız strateji momentum iken)."""
        if not self.is_momentum() or eq <= 0:
            return
        s = self.settings
        peak = max(float(db.kv_get(self._mk("peak"), 0.0) or 0.0), eq)
        db.kv_set(self._mk("peak"), peak)
        day = _utc_day(now)
        ds = db.kv_get(self._mk("day")) or {}
        if ds.get("day") != day:
            ds = {"day": day, "equity": eq}
            db.kv_set(self._mk("day"), ds)
        if self.guard.state()["halted"]:
            return
        dd = self.mom_drawdown_pct(eq)
        lim_dd = float(s.get("mom_max_dd_pct", 10.0))
        if dd >= lim_dd:
            reason = (f"Portföy zirveden %{dd:.1f} düştü (limit %{lim_dd:g}): tüm pozisyonlar kapatıldı, bot kilitlendi. "
                      f"Devam için panelden 'Kilidi kaldır'.")
            db.kv_set(f"risk:{self.mode}:halted", True)
            db.kv_set(f"risk:{self.mode}:halt_reason", reason)
            await self._mom_close_all("Maksimum drawdown")
            self.save_settings({"running": False})
            self.log("error", reason)
            self.notify(telegram.msg_alert("ACİL: MAKSİMUM DRAWDOWN", reason))
            return
        lim_day = float(s.get("mom_daily_loss_pct", 3.0))
        start = float(ds.get("equity") or eq)
        if start > 0 and eq <= start * (1 - lim_day / 100) and db.kv_get(self._mk("paused_day")) != day:
            db.kv_set(self._mk("paused_day"), day)
            loss = start - eq
            reason = (f"Günlük kayıp {loss:.2f} USDT (%{loss / start * 100:.1f}, limit %{lim_day:g}): pozisyonlar "
                      f"kapatıldı, yarın 00:00 UTC'ye (TR 03:00) kadar yeni pozisyon açılmayacak.")
            await self._mom_close_all("Günlük kayıp limiti")
            self.log("error", reason)
            self.notify(telegram.msg_alert("Günlük kayıp limiti", reason))

    async def _mom_close_all(self, reason: str) -> None:
        async with self.lock:
            for t in self.mom_trades():
                mark = self.md.mark(t["symbol"]) or t["entry_price"]
                try:
                    await self._close(t, mark, "market", reason)
                except Exception as e:  # noqa: BLE001
                    self.log("error", f"#{t['id']} kapatılamadı ({reason}): {e!r}")

    def mom_reset_peak(self) -> None:
        db.kv_set(self._mk("peak"), 0.0)

    # ------------------------------------------------------------------ panel
    def _mom_candidates(self, snap: dict) -> None:
        """Tarayıcı sekmesi: her coinin skoru ve durumu."""
        for sym, sc in sorted(snap["scores"].items(), key=lambda kv: -(kv[1] if kv[1] is not None else -99)):
            w = snap["targets"].get(sym)
            if w:
                status = f"{'LONG' if w > 0 else 'SHORT'} hedef: özsermayenin %{abs(w) * 100:.1f}'i"
            elif sym in snap["skipped"]:
                status = "Atlandı: " + snap["skipped"][sym]
            elif sym in snap["longs"] or sym in snap["shorts"]:
                status = "Seçildi ama açılmadı (bkz. notlar)"
            else:
                status = "Ortada: tutulmaz"
            self._candidate_raw({"symbol": sym, "side": "LONG" if (w or 0) > 0 else "SHORT" if w else "-",
                                 "close": None, "status": status,
                                 "reasons": [f"Momentum skoru {sc:+.2f}" if sc is not None else "Skor yok"]
                                 + [n for n in snap["notes"] if n.startswith(sym)][:2]})
