"""Bot çekirdeği: tarama, bekleyen girişler, pozisyon yönetimi, risk kilitleri, bildirimler."""
from __future__ import annotations

import asyncio
import os
import random
import socket
import time
import traceback
import uuid
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from . import backtest, coinglass, config, db, fng, telegram, trend
from .binance_client import BinanceError, BinanceREST
from .brokers import LiveBroker, PaperBroker
from .config import INTERVAL_MS
from .market import MarketData
from .risk import RiskGuard, compute_stats, size_position
from .strategy import SymbolAnalyzer, fmt_price

TZ_TR = ZoneInfo("Europe/Istanbul")

MODE_TR = {"paper": "Demo", "testnet": "Testnet", "live": "Gerçek"}
LEASE = "main"
LEASE_TTL_MS = 20_000
NOT_LEADER = ("Bot şu an Render'daki diğer kopyada çalışıyor (yeni sürüm yayına alınıyor). "
              "Birkaç saniye sonra tekrar dene.")


def _dir(side: str) -> int:
    return 1 if side == "LONG" else -1


class Bot:
    def __init__(self) -> None:
        self.settings = db.get_settings()
        self.md = MarketData(self.log)
        self.paper = PaperBroker(lambda: self.settings)
        self.broker = self.paper
        self.guard = RiskGuard(self.mode)
        self.lock = asyncio.Lock()
        self.tasks: list[asyncio.Task] = []
        self.candidates: deque = deque(maxlen=60)
        self.scan_info = {"last_scan": 0, "duration": 0.0, "scanned": 0, "signals": 0, "next_scan": 0, "status": "Başlatılıyor"}
        self.live_error = ""
        self.seen_keys: set[str] = set()
        self._funding_seen: dict[str, tuple[float, int]] = {}
        self._bal_cache = (0.0, 0.0)
        self._stats_cache = (0.0, None)
        self._last_bucket = None
        self._last_equity = 0.0
        self._last_poll = 0.0
        self.backtest_state = {"running": False, "done": 0, "total": 0, "symbol": "", "error": ""}
        self.started = time.time()
        self.iid = f"{os.environ.get('RENDER_INSTANCE_ID') or socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.is_leader = False
        self._bg: set[asyncio.Task] = set()
        self.fng_value: int | None = None

    # ------------------------------------------------------------------ yardımcılar
    @property
    def mode(self) -> str:
        return self.settings["mode"]

    @property
    def sim(self) -> bool:
        return self.md.sim is not None

    def now(self) -> int:
        return self.md.clock.now_ms()

    def log(self, level: str, msg: str) -> None:
        db.add_log(level, msg)
        print(f"[{level.upper()}] {msg}", flush=True)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def notify(self, text: str) -> None:
        if telegram.configured():
            self._spawn(self._notify(text))

    async def _notify(self, text: str) -> None:
        ok, err = await telegram.send(text)
        if not ok:
            self.log("warn", f"Telegram gönderilemedi: {err}")

    def save_settings(self, values: dict) -> dict:
        self.settings = db.save_settings(values)
        return self.settings

    # ------------------------------------------------------------------ yaşam döngüsü
    def _reload_keys(self) -> None:
        self.seen_keys = set()
        for t in db.list_trades(self.mode, None, limit=500):
            k = (t.get("meta") or {}).get("key")
            if k:
                self.seen_keys.add(k)

    async def start(self) -> None:
        self.log("info", f"Bot başlatıldı — mod: {MODE_TR[self.mode]}" + (" (SİMÜLASYON)" if self.sim else ""))
        self._reload_keys()
        await self._setup_mode(self.mode)
        await self.md.start()
        await self.md.poll_marks()
        await self._lease_tick()
        self.tasks = [asyncio.create_task(self._loop(self._lease_tick, 5.0, "lease")),
                      asyncio.create_task(self._loop(self._tick, 0.5 if self.sim else 1.0, "tick")),
                      asyncio.create_task(self._loop(self._scan_tick, 0.5 if self.sim else 2.0, "scan")),
                      asyncio.create_task(self._loop(self._slow_tick, 3.0 if self.sim else 30.0, "slow")),
                      asyncio.create_task(self._loop(self._sync_tick, 5.0, "sync"))]

    async def stop(self) -> None:
        for t in list(self.tasks) + list(self._bg):
            t.cancel()
        await asyncio.gather(*self.tasks, *self._bg, return_exceptions=True)
        self.tasks = []
        if self.is_leader:
            try:
                db.lease_release(LEASE, self.iid)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] kilit bırakılamadı: {e}", flush=True)
            self.is_leader = False
        if self.md.stream:
            self.md.stream.stop()
        if self.md._stream_task:
            self.md._stream_task.cancel()
        for rest in {self.md.rest, getattr(self.broker, "rest", None)}:
            if rest is not None:
                try:
                    await rest.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _lease_tick(self) -> None:
        """Yalnızca yönetim kilidini tutan kopya işlem yapar (Render deploy geçişinde çift işlem olmasın)."""
        got = await asyncio.to_thread(db.lease_acquire, LEASE, self.iid, LEASE_TTL_MS)
        if got and not self.is_leader:
            self.is_leader = True
            # diğer kopyanın yaptığı değişiklikleri veritabanından al
            old_mode = self.mode
            self.settings = db.get_settings()
            self._reload_keys()
            self._last_bucket = None
            if self.mode != old_mode:
                await self._setup_mode(self.mode)
            self.log("info", "Bot yönetimi bu sunucu kopyasında" + ("" if self.started > time.time() - 60 else " (devralındı)"))
        elif not got and self.is_leader:
            self.is_leader = False
            self.log("warn", "Bot yönetimi başka bir kopyaya geçti; bu kopya işlem yapmayacak")

    def _require_leader(self) -> None:
        if not self.is_leader:
            raise ValueError(NOT_LEADER)

    async def _loop(self, fn, interval: float, name: str) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("error", f"{name} döngüsü hatası: {e}")
                print(traceback.format_exc(), flush=True)
            await asyncio.sleep(interval)

    async def _setup_mode(self, mode: str) -> None:
        self.md.use_endpoint(mode)
        self.guard = RiskGuard(mode)
        self.live_error = ""
        if mode == "paper":
            self.broker = self.paper
            return
        if self.sim:
            self.broker = self.paper
            self.live_error = "Simülasyonda testnet/gerçek işlem yapılamaz (demo motoru kullanılıyor)"
            return
        key, secret, base = ((config.TESTNET_API_KEY, config.TESTNET_API_SECRET, config.TESTNET_REST)
                             if mode == "testnet" else (config.LIVE_API_KEY, config.LIVE_API_SECRET, config.MAINNET_REST))
        if not key or not secret:
            self.broker = None
            need = "SMCBOT_TESTNET_API_KEY (veya BINANCE_API_KEY)" if mode == "testnet" else "SMCBOT_LIVE_API_KEY / SMCBOT_LIVE_API_SECRET"
            self.live_error = f"{MODE_TR[mode]} API anahtarı tanımlı değil: Render > crypto-bot-dashboard > Environment > {need}"
            self.log("error", self.live_error)
            return
        lb = LiveBroker(mode, BinanceREST(base, key, secret), lambda: self.settings, self.log)
        err = await lb.check()
        self.broker = lb
        if err:
            self.live_error = err
            self.log("error", f"Binance hesap kontrolü: {err}")
        else:
            self.log("info", f"Binance {MODE_TR[mode]} hesabına bağlanıldı")

    # ------------------------------------------------------------------ bakiye
    async def balance(self) -> float:
        if self.broker is None:
            return 0.0
        if isinstance(self.broker, PaperBroker):
            return self.broker.balance()
        if time.time() - self._bal_cache[1] < 10:
            return self._bal_cache[0]
        try:
            b = await self.broker.get_balance()
            self._bal_cache = (b, time.time())
            return b
        except BinanceError as e:
            self.live_error = str(e)
            return self._bal_cache[0]

    def open_trades(self) -> list[dict]:
        return db.list_trades(self.mode, ("open",), limit=50)

    def pending_trades(self) -> list[dict]:
        return db.list_trades(self.mode, ("pending",), limit=50)

    def unrealized(self, t: dict) -> tuple[float, float]:
        mark = self.md.mark(t["symbol"])
        if not mark or not t.get("entry_price"):
            return 0.0, 0.0
        d = _dir(t["side"])
        u = (mark - t["entry_price"]) * (t.get("qty_open") or 0) * d
        ru = abs(t["entry_price"] - t["sl_initial"]) or 1e-12
        return u, (mark - t["entry_price"]) * d / ru

    async def equity(self) -> tuple[float, float, float]:
        bal = await self.balance()
        u = sum(self.unrealized(t)[0] for t in self.open_trades())
        return bal, bal + u, u

    # ------------------------------------------------------------------ tarama
    async def _scan_tick(self) -> None:
        if not self.is_leader:
            self.scan_info["status"] = "Yedekte (bot diğer kopyada çalışıyor)"
            return
        s = self.settings
        ims = INTERVAL_MS[self.scan_tf()]
        now = self.now()
        bucket = now // ims
        self.scan_info["next_scan"] = (bucket + 1) * ims + (0 if self.sim else 3000)
        if not s["running"]:
            self.scan_info["status"] = "Duraklatıldı"
            return
        if self.broker is None or (isinstance(self.broker, LiveBroker) and self.live_error):
            self.scan_info["status"] = "Hesap/bağlantı sorunu: " + (self.live_error or "işlem motoru hazır değil")
            return
        if bucket == self._last_bucket:
            return
        if not self.sim and now % ims < 3000 and self._last_bucket is not None:
            return  # mum kapanışından sonra birkaç sn bekle
        self._last_bucket = bucket
        await self.scan()

    def scan_tf(self) -> str:
        return "4h" if self.settings.get("strategy") == "trend" else self.settings["ltf"]

    async def scan(self) -> None:
        if self.settings.get("strategy") == "trend":
            await self.scan_trend()
            return
        s = self.settings
        t0 = time.time()
        self.scan_info["status"] = "Taranıyor…"
        await self.md.refresh_universe(int(s["universe_size"]))
        syms = list(self.md.universe)
        sem = asyncio.Semaphore(6)
        found: list = []
        near_list: list = []
        pre_thr = max(0.0, s["min_score"] - 25)  # akış verisi en fazla ~25 puan ekleyebilir

        async def one(sym: str) -> None:
            async with sem:
                ltf = await self.md.candles(sym, s["ltf"], 300)
                htf = await self.md.candles(sym, s["htf"], 200)
                if ltf is None or htf is None:
                    return
                an = SymbolAnalyzer(sym, ltf, htf)
                t = len(ltf) - 1
                sig, near = await asyncio.to_thread(an.evaluate, t, s, None, pre_thr)
                if sig is None:
                    if near is not None and near.score >= s["min_score"] - 35:
                        near_list.append(near)
                    return
                fl = await self.md.flow(sym)
                sig2, near2 = an.evaluate(t, s, fl)
                if sig2:
                    found.append(sig2)
                elif near2:
                    near_list.append(near2)

        await asyncio.gather(*(one(x) for x in syms))
        for n in near_list:
            self._candidate(n, f"Eşik altı ({n.score:.0f} < {s['min_score']})")
        async with self.lock:
            for sig in sorted(found, key=lambda x: -x.score):
                await self._register_signal(sig)
        dt = time.time() - t0
        self.scan_info.update(last_scan=self.now(), duration=round(dt, 2), scanned=len(syms),
                              signals=len(found), status="Bekliyor (sonraki mum kapanışı)")
        if found or near_list:
            self.log("info", f"Tarama: {len(syms)} coin, {len(found)} sinyal, {len(near_list)} yakın aday ({dt:.1f} sn)")

    async def scan_trend(self) -> None:
        """4s trend takibi: kapanan her 4s mumda kırılımları ara ve açık işlemlerin iz süren stoplarını güncelle."""
        s = self.settings
        t0 = time.time()
        self.scan_info["status"] = "Taranıyor…"
        await self.md.refresh_universe(int(s["universe_size"]))
        syms = list(self.md.universe)
        self.fng_value = await fng.latest(self.sim)
        sem = asyncio.Semaphore(6)
        found: list = []
        cands: list[dict] = []
        c4cache: dict = {}

        async def one(sym: str) -> None:
            async with sem:
                c4 = await self.md.candles(sym, "4h", 120)
                if c4 is None:
                    return
                c4cache[sym] = c4
                sigs, cs = trend.evaluate(sym, c4, s, self.fng_value)
                found.extend(sigs)
                cands.extend(cs)

        await asyncio.gather(*(one(x) for x in syms))
        async with self.lock:
            for t in self.open_trades():
                if (t.get("meta") or {}).get("exit_mode") != "trail":
                    continue
                c4 = c4cache.get(t["symbol"]) or await self.md.candles(t["symbol"], "4h", 120)
                if c4 is not None:
                    await self._trail_update(t, c4)
            for c in cands:
                self._candidate_raw(c)
            now = self.now()
            opened, skipped = 0, {}
            per_bar = int(s.get("trend_max_new_per_bar", 0) or 0)
            random.shuffle(found)  # aynı anda kırılanlardan hangisinin alınacağı rastgele (geçmiş testteki gibi)
            for sig in found:
                late_min = (now - sig.setup_time) / 60_000
                if per_bar and opened >= per_bar and sig.key not in self.seen_keys:
                    why = f"bu 4s mumda zaten {per_bar} yeni giriş yapıldı"
                    self._candidate(sig, f"Atlandı: {why} (aynı anda kırılan coinler çoğu zaman tek bir piyasa hareketidir)")
                    self.seen_keys.add(sig.key)
                elif late_min > 15:
                    # bot yeniden başladığında son kapanmış mum eski olabilir: geç girilmez (test edilen kural:
                    # kapanışın hemen ardından giriş). İşlem olarak kaydetme, sadece tarayıcıda göster.
                    self._candidate(sig, f"Geç kalındı: mum {late_min:.0f} dk önce kapandı, girilmedi")
                    self.seen_keys.add(sig.key)
                    why = "geç kalındı (bot yeni başladı)"
                else:
                    why = await self._register_signal(sig)
                if why is None:
                    opened += 1
                elif why != "daha önce görüldü":
                    skipped[why] = skipped.get(why, 0) + 1
        dt = time.time() - t0
        summary = f"son taramada {len(found)} kırılım"
        if found:
            parts = ([f"{opened} işlem açıldı"] if opened else []) + [f"{k} atlandı: {v}" for v, k in skipped.items()]
            summary += " · " + ", ".join(parts)
        self.scan_info.update(last_scan=self.now(), duration=round(dt, 2), scanned=len(syms), signals=len(found),
                              opened=opened, skipped=skipped, summary=summary,
                              status="Bekliyor (sonraki 4 saatlik mum kapanışı)")
        self.log("info", f"4s tarama: {len(syms)} coin, {len(found)} kırılım, korku endeksi "
                         f"{self.fng_value if self.fng_value is not None else '—'} ({dt:.1f} sn)")

    async def _trail_update(self, t: dict, c4) -> None:
        new = trend.new_trail_stop(t, c4, int(self.settings["trend_exit_n"]))
        if new is None:
            return
        sym = self.md.symbols.get(t["symbol"])
        if sym:
            new = sym.round_price(new)
            if new == t["sl"]:
                return
        meta = dict(t.get("meta") or {})
        if isinstance(self.broker, LiveBroker):
            try:
                meta.update(await self.broker.move_stop(dict(t, meta=meta), new, sym))
            except BinanceError as e:
                self.log("error", f"#{t['id']} iz süren stop taşınamadı: {e} (eski stop yerinde)")
                return
        meta["trail"] = (meta.get("trail") or [])[-59:] + [[self.now(), new]]
        db.update_trade(t["id"], sl=new, meta=meta)
        self.log("info", f"#{t['id']} {t['symbol']} {t['side']} iz süren stop: {fmt_price(t['sl'])} → {fmt_price(new)}")

    async def _maybe_daily_summary(self, now: int) -> None:
        """Her gün 23:05'ten sonra (TR saati, 23:00 taramasının ardından) bir kez Telegram'a özet gönderir."""
        if not self.settings.get("telegram_daily_summary", True) or not telegram.configured():
            return
        lt = datetime.fromtimestamp(now / 1000, TZ_TR)
        day = lt.strftime("%Y-%m-%d")
        key = f"tg:summary_day:{self.mode}"
        if (lt.hour, lt.minute) < (23, 5) or db.kv_get(key) == day:
            return
        db.kv_set(key, day)
        try:
            self.notify(await self.daily_summary())
        except Exception as e:  # noqa: BLE001
            self.log("warn", f"Günlük özet hazırlanamadı: {e}")

    async def daily_summary(self) -> str:
        now = self.now()
        lt = datetime.fromtimestamp(now / 1000, TZ_TR)
        day0 = int(lt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        bal, eq, upnl = await self.equity()
        closed = [t for t in db.list_trades(self.mode, ("closed",), limit=300) if (t.get("exit_time") or 0) >= day0]
        positions = []
        for t in self.open_trades():
            u, ur = self.unrealized(t)
            positions.append({**t, "upnl": u, "upnl_r": ur})
        g = self.guard.state()
        return telegram.msg_daily({
            "date": lt.strftime("%d.%m.%Y"), "mode": self.mode, "equity": eq, "upnl": upnl,
            "start": self.paper.start_balance() if self.mode == "paper" else None,
            "closed": sorted(closed, key=lambda t: t.get("exit_time") or 0), "positions": positions,
            "max_open": int(self.settings["max_open_positions"]), "scan_summary": self.scan_info.get("summary"),
            "running": self.settings["running"], "halted": g["halt_reason"] if g["halted"] else None,
        })

    def _candidate_raw(self, c: dict) -> None:
        self.candidates.appendleft({
            "time": self.now(), "symbol": c["symbol"], "side": c["side"], "score": None, "structure": None,
            "flow": None, "entry": c.get("close"), "stop": None, "tp2": None, "rr": None, "status": c["status"],
            "reasons": c.get("reasons") or [f"Kırılım seviyesi {fmt_price(c['level'])}, son kapanış {fmt_price(c['close'])}"],
        })

    def _candidate(self, sig, status: str) -> None:
        if (sig.annotations or {}).get("strategy") == "trend":
            self.candidates.appendleft({
                "time": self.now(), "symbol": sig.symbol, "side": sig.side, "score": None, "structure": None,
                "flow": None, "entry": sig.entry, "stop": sig.stop, "tp2": None, "rr": None, "status": status,
                "reasons": [r["text"] for r in sig.reasons][:4]})
            return
        self.candidates.appendleft({
            "time": self.now(), "symbol": sig.symbol, "side": sig.side, "score": sig.score,
            "structure": sig.structure_score, "flow": sig.flow_score, "entry": sig.entry, "stop": sig.stop,
            "tp2": sig.tp2, "rr": sig.rr, "status": status,
            "reasons": [r["text"] for r in sig.reasons if r.get("ok")][:6],
        })

    async def _register_signal(self, sig) -> str | None:
        """Sinyali işleme dönüştürür. Dönüş: None = kaydedildi, metin = neden atlandığı."""
        s = self.settings
        if sig.key in self.seen_keys:
            return "daha önce görüldü"
        self.seen_keys.add(sig.key)
        active = db.list_trades(self.mode, ("open", "pending"), limit=50)
        if any(t["symbol"] == sig.symbol for t in active):
            self._candidate(sig, "Atlandı: bu coinde açık/bekleyen işlem var")
            return "bu coinde zaten işlem var"
        ok, why = self.guard.can_open(sig.side, sig.symbol, active, self.now(), s)
        if ok:
            pend_same = sum(1 for t in active if t["status"] == "pending")
            opens = sum(1 for t in active if t["status"] == "open")
            if opens + pend_same >= int(s["max_open_positions"]) + 1:
                ok, why = False, "Bekleyen giriş sayısı dolu"
        if not ok:
            self._candidate(sig, f"Risk engeli: {why}")
            return why
        ann = dict(sig.annotations)
        if not ann.get("tf"):
            ann["tf"] = s["ltf"]
        is_trend = ann.get("strategy") == "trend"
        tid = db.insert_trade({
            "mode": self.mode, "symbol": sig.symbol, "side": sig.side, "status": "pending", "score": sig.score,
            "setup_time": sig.setup_time, "entry_price": sig.entry, "sl": sig.stop, "sl_initial": sig.stop,
            "tp1": sig.tp1, "tp2": sig.tp2, "leverage": int(s["leverage"]), "reasons": sig.reasons,
            "annotations": ann, "meta": {"key": sig.key, "expiry_time": sig.expiry_time, "rr": sig.rr,
                                         "stop_pct": sig.stop_pct, "structure_score": sig.structure_score,
                                         "flow_score": sig.flow_score, "planned_entry": sig.entry,
                                         **({"exit_mode": "trail", "market": True} if is_trend else {})}})
        if is_trend:
            self._candidate(sig, f"Kırılım: piyasa emriyle giriliyor (#{tid})")
            self.log("info", f"#{tid} {sig.symbol} {sig.side} 4s kırılım: kapanış {fmt_price(sig.entry)}, "
                             f"stop {fmt_price(sig.stop)} (%{sig.stop_pct:.1f}), hedef yok (iz süren stop)")
            await self._process_pending()  # beklemeden hemen gir (fiyat yoksa sonraki tikte)
        else:
            self._candidate(sig, f"Giriş bekleniyor (#{tid})")
            self.log("info", f"#{tid} {sig.symbol} {sig.side} kurulumu: giriş {fmt_price(sig.entry)}, stop {fmt_price(sig.stop)}, "
                             f"hedef {fmt_price(sig.tp2)} (RR {sig.rr}, skor {sig.score:.0f})")
        if s["notify_setups"]:
            self.notify(telegram.msg_setup(sig.to_dict(), self.mode))
        return None

    # ------------------------------------------------------------------ fiyat döngüsü
    async def _tick(self) -> None:
        # Simülasyonda her tikte, gerçekte WebSocket koptuysa 3 sn'de bir REST ile fiyat çek
        if self.sim or (not self.md.ws_ok() and time.time() - self._last_poll >= 3):
            self._last_poll = time.time()
            await self.md.poll_marks()
        if not self.is_leader:
            return
        async with self.lock:
            await self._process_pending()
            await self._process_open()

    async def _process_pending(self) -> None:
        now = self.now()
        for t in self.pending_trades():
            mark = self.md.mark(t["symbol"])
            if mark is None:
                continue
            L = t["side"] == "LONG"
            meta = t.get("meta") or {}
            if meta.get("market"):  # trend kırılımı: kapanışın hemen ardından piyasa emri
                if now >= int(meta.get("expiry_time", now + 1)):
                    self._cancel(t, "Süre doldu: giriş yapılamadı")
                elif (mark <= t["sl"]) if L else (mark >= t["sl"]):
                    self._cancel(t, "Fiyat girişten önce stop seviyesini geçti (kırılım başarısız)")
                else:
                    await self._open(t, mark)
                continue
            if now >= int(meta.get("expiry_time", now + 1)):
                self._cancel(t, "Süre doldu: fiyat giriş seviyesine gelmedi")
            elif (mark <= t["sl"]) if L else (mark >= t["sl"]):
                self._cancel(t, "Giriş olmadan stop seviyesi bozuldu (kurulum geçersiz)")
            elif (mark >= t["tp1"]) if L else (mark <= t["tp1"]):
                self._cancel(t, "Fiyat girişe gelmeden hedefe doğru gitti (kaçırıldı)")
            elif (mark <= t["entry_price"]) if L else (mark >= t["entry_price"]):
                await self._open(t, mark)
            elif self.settings.get("entry_mode") == "market":
                await self._open(t, mark)  # geri çekilme beklenmez; stop ile TP1 arasındaysa hemen gir

    def _cancel(self, t: dict, reason: str) -> None:
        db.update_trade(t["id"], status="cancelled", exit_reason=reason, exit_time=self.now())
        self.log("info", f"#{t['id']} {t['symbol']} {t['side']} iptal: {reason}")

    async def _open(self, t: dict, mark: float) -> None:
        s = self.settings
        if not s["running"]:
            self._cancel(t, "Bot duraklatıldı")
            return
        ok, why = self.guard.can_open(t["side"], t["symbol"], self.open_trades(), self.now(), s)
        if not ok:
            self._cancel(t, f"Risk engeli: {why}")
            return
        bal = await self.balance()
        sym = self.md.symbols.get(t["symbol"])
        sz = size_position(bal, mark, t["sl"], s, sym)
        if not sz:
            self._cancel(t, "Pozisyon büyüklüğü borsa minimumunun altında")
            return
        t["leverage"] = sz["leverage"]
        try:
            fill = await self.broker.open(t, sz["qty"], mark, sym)
        except BinanceError as e:
            self._cancel(t, f"Emir hatası: {e.msg}")
            self.notify(telegram.msg_alert("Emir açılamadı", f"{t['symbol']}: {e.msg}"))
            return
        entry = fill["price"]
        qty = fill["qty"]
        per_unit = abs(entry - t["sl"]) + entry * (2 * s["taker_fee_pct"] + s["slippage_pct"]) / 100
        meta = dict(t.get("meta") or {})
        meta.update(fill.get("meta") or {})
        if sz["capped"]:
            meta["capped"] = True
        upd = dict(status="open", entry_time=self.now(), entry_price=entry, qty=qty, qty_open=qty,
                   notional=entry * qty, leverage=sz["leverage"], margin=entry * qty / sz["leverage"],
                   risk_amount=per_unit * qty, fees=fill["fee"], pnl=-fill["fee"], meta=meta)
        db.update_trade(t["id"], **upd)
        t.update(upd)
        cap = " (teminat sınırı nedeniyle risk düşürüldü)" if sz["capped"] else ""
        self.log("info", f"#{t['id']} {t['symbol']} {t['side']} AÇILDI @ {fmt_price(entry)}, miktar {qty:g}, "
                         f"risk {per_unit * qty:.2f} USDT{cap}")
        self.notify(telegram.msg_opened(t, self.mode))

    async def _process_open(self) -> None:
        paper = isinstance(self.broker, PaperBroker)
        for t in self.open_trades():
            mark = self.md.mark(t["symbol"])
            if mark is None:
                continue
            L = t["side"] == "LONG"
            d = _dir(t["side"])
            ru = abs(t["entry_price"] - t["sl_initial"]) or 1e-12
            r_now = (mark - t["entry_price"]) * d / ru
            if r_now > (t.get("mfe") or 0) + 0.02 or -r_now > (t.get("mae") or 0) + 0.02:
                db.update_trade(t["id"], mfe=max(t.get("mfe") or 0, r_now), mae=max(t.get("mae") or 0, -r_now))
            if (t.get("meta") or {}).get("exit_mode") == "trail":
                # trend: hedef yok, sadece (iz süren) stop. Gerçek/testnette stop borsada bekler.
                if paper and ((mark <= t["sl"]) if L else (mark >= t["sl"])):
                    profit = (t["sl"] - t["entry_price"]) * d > 0
                    await self._close(t, mark, "sl", "İz süren stop (kârda)" if profit else "Stop")
                continue
            hit_tp1 = not t["tp1_hit"] and ((mark >= t["tp1"]) if L else (mark <= t["tp1"]))
            if paper:
                if (mark <= t["sl"]) if L else (mark >= t["sl"]):
                    await self._close(t, mark, "sl", "Başabaş stop (TP1 sonrası)" if t["tp1_hit"] else "Stop")
                    continue
                if (mark >= t["tp2"]) if L else (mark <= t["tp2"]):
                    if not t["tp1_hit"]:
                        await self._take_tp1(t, t["tp1"])
                        t = db.get_trade(t["id"])
                    await self._close(t, t["tp2"], "tp", "TP2 hedef (likidite)")
                    continue
                if hit_tp1:
                    await self._take_tp1(t, t["tp1"])
            elif hit_tp1:
                await self._take_tp1(t, mark)

    async def _take_tp1(self, t: dict, ref: float) -> None:
        s = self.settings
        sym = self.md.symbols.get(t["symbol"])
        qty = t["qty"] * s["tp1_close_pct"] / 100
        if sym:
            qty = sym.round_qty(qty)
        d = _dir(t["side"])
        be = t["entry_price"] * (1 + 2 * s["taker_fee_pct"] / 100 * d)
        if sym:
            be = sym.round_price(be)
        meta = dict(t.get("meta") or {})
        pnl_add = fee_add = 0.0
        if 0 < qty < (t["qty_open"] or 0):
            try:
                res = await self.broker.reduce(t, qty, ref, "tp" if isinstance(self.broker, PaperBroker) else "partial", sym)
            except BinanceError as e:
                self.log("error", f"#{t['id']} TP1 kısmi kapatma hatası: {e}")
                return
            qty = res.get("qty", qty)
            pnl_add, fee_add = res["pnl"], res["fee"]
            meta.setdefault("partials", []).append({"time": self.now(), "price": res["price"], "qty": qty})
            meta["last_reduce_time"] = self.now()
        else:
            qty = 0.0
        try:
            m2 = await self.broker.move_stop(dict(t, meta=meta), be, sym)
            meta.update(m2)
        except BinanceError as e:
            self.log("error", f"#{t['id']} stop girişe çekilemedi: {e} (eski stop yerinde)")
            be = t["sl"]
        rest = (t["qty_open"] or 0) - qty
        rest = sym.round_qty(rest) if sym else round(rest, 8)
        db.update_trade(t["id"], tp1_hit=1, qty_open=rest, sl=be,
                        pnl=(t["pnl"] or 0) + pnl_add - fee_add, fees=(t["fees"] or 0) + fee_add, meta=meta)
        self.log("info", f"#{t['id']} {t['symbol']} TP1: {qty:g} kapatıldı ({pnl_add - fee_add:+.2f} USDT), stop girişe çekildi")
        self.notify(telegram.msg_tp1(dict(t, tp1_close_pct=s["tp1_close_pct"]), self.mode, pnl_add - fee_add))

    async def _close(self, t: dict, ref: float, kind: str, reason: str) -> None:
        sym = self.md.symbols.get(t["symbol"])
        try:
            res = await self.broker.reduce(t, t["qty_open"] or 0, ref, kind if isinstance(self.broker, PaperBroker) else "market", sym)
        except BinanceError as e:
            self.log("error", f"#{t['id']} kapatma hatası: {e}")
            self.notify(telegram.msg_alert("Pozisyon kapatılamadı!", f"{t['symbol']}: {e.msg} — Binance'ten kontrol et"))
            return
        self._finalize(t, res["price"], res["pnl"], res["fee"], reason, res.get("qty", t["qty_open"]))

    def _finalize(self, t: dict, last_price: float, pnl_part: float, fee_part: float, reason: str, last_qty: float | None) -> None:
        meta = t.get("meta") or {}
        parts = meta.get("partials", [])
        q_last = last_qty if last_qty else (t["qty_open"] or 0)
        tot_q = sum(p["qty"] for p in parts) + q_last
        avg = ((sum(p["price"] * p["qty"] for p in parts) + last_price * q_last) / tot_q) if tot_q else last_price
        pnl = (t["pnl"] or 0) + pnl_part - fee_part
        fees = (t["fees"] or 0) + fee_part
        r = pnl / t["risk_amount"] if t.get("risk_amount") else 0.0
        db.update_trade(t["id"], status="closed", exit_time=self.now(), exit_price=avg, pnl=pnl, fees=fees,
                        r_multiple=r, exit_reason=reason, qty_open=0)
        t2 = db.get_trade(t["id"])
        self.log("info" if pnl >= 0 else "warn",
                 f"#{t['id']} {t['symbol']} {t['side']} KAPANDI: {pnl:+.2f} USDT ({r:+.2f}R) — {reason}")
        self.notify(telegram.msg_closed(t2, self.mode))
        alert = self.guard.register_close(self.now(), self.settings)
        if alert:
            self.log("warn", alert)
            self.notify(telegram.msg_alert("Risk koruması", alert))
        self._stats_cache = (0.0, None)

    # ------------------------------------------------------------------ canlı senkron
    async def _sync_tick(self) -> None:
        if not self.is_leader:
            return
        if isinstance(self.broker, LiveBroker) and not self.live_error:
            opens = self.open_trades()
            if not opens:
                return
            async with self.lock:
                try:
                    closed = await self.broker.sync(self.open_trades())
                except BinanceError as e:
                    self.log("warn", f"Senkron hatası: {e}")
                    return
                for c in closed:
                    t = c["trade"]
                    px = c["price"]
                    d = _dir(t["side"])
                    if t.get("tp2") is None:
                        reason = "İz süren stop (borsa)" if (px - t["entry_price"]) * d > 0 else "Stop (borsa)"
                    elif abs(px - t["sl"]) <= abs(px - t["tp2"]):
                        reason = "Başabaş stop (TP1 sonrası)" if t["tp1_hit"] else "Stop (borsa)"
                    else:
                        reason = "TP2 hedef (borsa)"
                    if (px - t["entry_price"]) * d < 0 and t.get("sl") and abs(px - t["sl"]) / t["sl"] > 0.01:
                        reason = "Borsada kapatıldı (elle/likidasyon?)"
                    self._finalize(t, px, c["pnl"], c["fee"], reason, c["qty"] or None)

    # ------------------------------------------------------------------ yavaş döngü
    async def _slow_tick(self) -> None:
        if not self.is_leader:
            return
        s = self.settings
        now = self.now()
        if self.md.sim is None:
            await self.md.refresh_universe(int(s["universe_size"]))
        # Demo funding uygulaması
        if isinstance(self.broker, PaperBroker):
            opens = self.open_trades()
            for sym, (rate, nxt) in list(self.md.funding.items()):
                prev = self._funding_seen.get(sym)
                if prev and nxt > prev[1] and prev[1] <= now:
                    for t in opens:
                        if t["symbol"] == sym and (t["entry_time"] or 0) < prev[1]:
                            mark = self.md.mark(sym) or t["entry_price"]
                            cost = (t["qty_open"] or 0) * mark * prev[0] * _dir(t["side"])
                            await self.broker.apply_funding(cost)
                            db.update_trade(t["id"], funding=(t["funding"] or 0) + cost, pnl=(t["pnl"] or 0) - cost)
                self._funding_seen[sym] = (rate, nxt)
        bal, eq, _ = await self.equity()
        if bal > 0:
            alert = self.guard.update(bal, eq, now, s)
            if alert:
                self.log("error", alert + " — Bot durduruldu. Panelden 'Kilidi kaldır' ile devam edebilirsin.")
                self.notify(telegram.msg_alert("BOT DURDURULDU", alert))
                self.save_settings({"running": False})
                for t in self.pending_trades():
                    self._cancel(t, "Risk kilidi")
            await self._maybe_daily_summary(now)
            if time.time() - self._last_equity >= (20 if self.sim else 300):
                db.add_equity_point(self.mode, bal, eq, now)
                self._last_equity = time.time()

    # ------------------------------------------------------------------ panel eylemleri
    async def set_running(self, running: bool) -> None:
        self._require_leader()
        if running and self.guard.state()["halted"]:
            raise ValueError("Risk kilidi aktif. Önce 'Kilidi kaldır'.")
        if running and (self.broker is None or (isinstance(self.broker, LiveBroker) and self.live_error)):
            raise ValueError(self.live_error or "İşlem motoru hazır değil")
        self.save_settings({"running": running})
        if not running:
            async with self.lock:
                for t in self.pending_trades():
                    self._cancel(t, "Bot duraklatıldı")
        else:
            self._last_bucket = None
        self.log("info", "Bot " + ("BAŞLATILDI: yeni işlem aranıyor" if running else "DURAKLATILDI: yeni işlem açılmayacak, açık pozisyonlar yönetilmeye devam ediyor"))

    async def close_trade(self, trade_id: int, reason: str = "Manuel kapatma") -> None:
        self._require_leader()
        async with self.lock:
            t = db.get_trade(trade_id)
            if not t or t["mode"] != self.mode:
                raise ValueError("İşlem bulunamadı")
            if t["status"] == "pending":
                self._cancel(t, "Manuel iptal")
                return
            if t["status"] != "open":
                raise ValueError("İşlem açık değil")
            mark = self.md.mark(t["symbol"]) or t["entry_price"]
            await self._close(t, mark, "market", reason)

    async def emergency(self) -> None:
        self._require_leader()
        self.save_settings({"running": False})
        async with self.lock:
            for t in self.pending_trades():
                self._cancel(t, "Acil durdurma")
            opens = self.open_trades()
        for t in opens:
            try:
                await self.close_trade(t["id"], "Acil durdurma")
            except Exception as e:  # noqa: BLE001
                self.log("error", f"Acil kapatma #{t['id']}: {e}")
        self.log("warn", f"ACİL DURDURMA: {len(opens)} pozisyon kapatıldı, bot durdu")
        self.notify(telegram.msg_alert("ACİL DURDURMA", f"{len(opens)} pozisyon kapatıldı, bot durduruldu."))

    async def reset(self) -> None:
        self._require_leader()
        if self.mode == "live":
            raise ValueError("Gerçek modda sıfırlama yapılamaz (gerçek para geçmişi silinmez)")
        if self.mode == "testnet" and self.open_trades():
            raise ValueError("Önce testnet pozisyonlarını kapat")
        async with self.lock:
            self.save_settings({"running": False})
            db.delete_mode_data(self.mode)
            self.guard.reset()
            if self.mode == "paper":
                self.paper.reset()
            self.candidates.clear()
            self.seen_keys.clear()
            self._stats_cache = (0.0, None)
        self.log("warn", f"{MODE_TR[self.mode]} hesabı sıfırlandı")

    def gate(self) -> dict:
        s = self.settings
        st = compute_stats(db.list_trades("paper", ("closed",), limit=5000), self.paper.start_balance())
        ok_n = st["trades"] >= int(s["live_unlock_min_trades"])
        pf = st["profit_factor"] or 0
        ok_pf = pf >= float(s["live_unlock_min_pf"])
        ok_pnl = st["net_pnl"] > 0
        return {"trades": st["trades"], "need_trades": int(s["live_unlock_min_trades"]), "pf": pf,
                "need_pf": float(s["live_unlock_min_pf"]), "net_pnl": st["net_pnl"],
                "passed": (ok_n and ok_pf and ok_pnl) or config.ALLOW_LIVE_WITHOUT_GATE,
                "override": config.ALLOW_LIVE_WITHOUT_GATE}

    async def switch_mode(self, mode: str, confirm: str = "") -> None:
        self._require_leader()
        if mode not in ("paper", "testnet", "live"):
            raise ValueError("Geçersiz mod")
        if mode == self.mode:
            return
        if db.list_trades(self.mode, ("open", "pending"), limit=5):
            raise ValueError("Mod değiştirmeden önce açık ve bekleyen işlemleri kapat")
        if mode == "live":
            g = self.gate()
            if not g["passed"]:
                raise ValueError(f"Gerçek mod kilitli: demoda en az {g['need_trades']} işlem, pozitif K/Z ve profit factor ≥ "
                                 f"{g['need_pf']} gerekli (şu an {g['trades']} işlem, PF {g['pf']:.2f}, K/Z {g['net_pnl']:.2f})")
            if confirm.strip().upper() != "GERÇEK":
                raise ValueError("Onay için GERÇEK yaz")
        async with self.lock:
            self.save_settings({"mode": mode, "running": False})
            self._last_bucket = None
            self.candidates.clear()
            await self._setup_mode(mode)
            await self.md.refresh_universe(int(self.settings["universe_size"]), force=True)
        self.log("warn", f"Mod değişti: {MODE_TR[mode]}")
        self.notify(telegram.msg_alert("Mod değişti", f"Yeni mod: {MODE_TR[mode]}"))

    def unlock_risk(self) -> None:
        self._require_leader()
        self.guard.unlock()
        st = self.guard.state()
        db.kv_set(f"risk:{self.mode}:peak_equity", 0.0)  # zirveyi şimdiki equity'den yeniden başlat
        self.log("warn", "Risk kilidi elle kaldırıldı" + (f" ({st['halt_reason']})" if st["halt_reason"] else ""))

    # ------------------------------------------------------------------ geçmiş test
    def start_backtest(self, days: int, n_symbols: int) -> None:
        self._require_leader()
        if self.backtest_state["running"]:
            raise ValueError("Geçmiş test zaten çalışıyor")
        days = max(3, min(365 if self.settings.get("strategy") == "trend" else 90, int(days)))
        n_symbols = max(1, min(40, int(n_symbols)))
        syms = list(self.md.universe)[:n_symbols]
        if not syms:
            raise ValueError("Coin listesi henüz yüklenmedi")
        self.backtest_state = {"running": True, "done": 0, "total": len(syms), "symbol": "", "error": ""}

        def prog(done: int, total: int, sym: str) -> None:
            self.backtest_state.update(done=done, total=total, symbol=sym)

        async def job() -> None:
            try:
                res = await backtest.run(self.md, dict(self.settings), days, syms, prog)
                db.save_backtest(res["params"], res)
                st = res["stats"]
                self.log("info", f"Geçmiş test bitti: {st['trades']} işlem, kazanma %{st['winrate']:.0f}, "
                                 f"PF {st['profit_factor'] or 0:.2f}, getiri %{st['return_pct']:.1f}, maks. düşüş %{st['max_drawdown_pct']:.1f}")
            except Exception as e:  # noqa: BLE001
                self.backtest_state["error"] = str(e)
                self.log("error", f"Geçmiş test hatası: {e}")
            finally:
                self.backtest_state["running"] = False

        self._spawn(job())

    # ------------------------------------------------------------------ panel durumu
    def stats(self) -> dict:
        if time.time() - self._stats_cache[0] < 5 and self._stats_cache[1]:
            return self._stats_cache[1]
        start = self.paper.start_balance() if self.mode == "paper" else None
        st = compute_stats(db.list_trades(self.mode, ("closed",), limit=5000), start)
        self._stats_cache = (time.time(), st)
        return st

    async def state(self) -> dict:
        s = self.settings
        bal, eq, upnl = await self.equity()
        positions = []
        for t in self.open_trades():
            u, ur = self.unrealized(t)
            positions.append({**_slim(t), "mark": self.md.mark(t["symbol"]), "upnl": u, "upnl_r": ur})
        pend = []
        for t in self.pending_trades():
            pend.append({**_slim(t), "mark": self.md.mark(t["symbol"]),
                         "expiry_time": (t.get("meta") or {}).get("expiry_time")})
        g = self.guard.state()
        dsb = g["day_start_balance"] or bal
        day_pnl = self.guard.daily_pnl(self.now())
        return {
            "mode": self.mode, "mode_tr": MODE_TR[self.mode], "running": s["running"], "sim": self.sim,
            "leader": self.is_leader, "strategy": s.get("strategy", "smc"), "fng": self.fng_value,
            "server_time": self.now(), "balance": bal, "equity": eq, "upnl": upnl,
            "day_pnl": day_pnl, "day_limit": -dsb * s["daily_loss_limit_pct"] / 100,
            "start_balance": self.paper.start_balance() if self.mode == "paper" else None,
            "positions": positions, "pending": pend,
            "limits": {"max_open": int(s["max_open_positions"]), "max_dir": int(s["max_same_direction"]),
                       "per_bar": int(s.get("trend_max_new_per_bar", 0) or 0)},
            "risk": {"halted": g["halted"], "halt_reason": g["halt_reason"], "cooldown_until": g["cooldown_until"],
                     "peak_equity": g["peak_equity"],
                     "drawdown_pct": ((g["peak_equity"] - eq) / g["peak_equity"] * 100) if g["peak_equity"] else 0.0},
            "scan": {**self.scan_info, "universe": len(self.md.universe), "ltf": self.scan_tf(),
                     "htf": "4h" if s.get("strategy") == "trend" else s["htf"]},
            "candidates": list(self.candidates)[:40],
            "health": {"ws": self.md.ws_ok(), "geo_blocked": self.md.geo_blocked, "data_error": self.md.last_error,
                       "live_error": self.live_error, "telegram": telegram.configured(), "coinglass": coinglass.status(),
                       "weight": getattr(self.md.rest, "used_weight", 0),
                       "liq_24h": self.md.liq.total_24h(), "uptime": int(time.time() - self.started)},
            "stats": self.stats(), "gate": self.gate() if self.mode != "live" else None,
            "backtest": self.backtest_state,
            "keys": {"testnet": bool(config.TESTNET_API_KEY), "live": bool(config.LIVE_API_KEY)},
        }


def _slim(t: dict) -> dict:
    keys = ("id", "symbol", "side", "status", "score", "setup_time", "entry_time", "entry_price", "qty", "qty_open",
            "notional", "leverage", "margin", "risk_amount", "sl", "sl_initial", "tp1", "tp2", "tp1_hit", "pnl", "fees",
            "mfe", "mae")
    return {k: t.get(k) for k in keys}
