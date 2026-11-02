"""Piyasa verisi servisi: coin evreni, mumlar, canlı mark fiyatları, likidasyonlar, akış verisi."""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from . import coinglass, config
from .binance_client import BinanceError, BinanceREST, MarketStream, SymbolInfo, parse_klines
from .orderflow import FlowSnapshot, LiquidationTracker, oi_change_pct, orderbook_imbalance
from .smc import Candles


class RealClock:
    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)


class MarketData:
    def __init__(self, log: Callable[[str, str], None]):
        self.log = log
        self.sim = None
        if config.OFFLINE_SIM:
            from .sim_market import SimREST
            self.sim = SimREST()
            self.rest = self.sim
            self.clock = self.sim.clock
        else:
            self.rest = BinanceREST(config.MAINNET_REST)
            self.clock = RealClock()
        self.base_url = config.MAINNET_REST
        self.symbols: dict[str, SymbolInfo] = {}
        self.universe: list[str] = []
        self.marks: dict[str, tuple[float, int]] = {}
        self.funding: dict[str, tuple[float, int]] = {}
        self.liq = LiquidationTracker(self.clock.now_ms)
        self.stream: MarketStream | None = None
        self._stream_task: asyncio.Task | None = None
        self._kcache: dict[tuple[str, str], tuple[int, dict]] = {}
        self.geo_blocked = False
        self.last_error = ""
        self.last_universe = 0.0

    # ------------------------------------------------------------------
    def use_endpoint(self, mode: str) -> None:
        """Testnet modunda veriler de testnetten okunur (fiyatlar gerçek piyasadan farklıdır)."""
        if self.sim:
            return
        url = config.TESTNET_REST if mode == "testnet" else config.MAINNET_REST
        if url != self.base_url:
            self.base_url = url
            self.rest = BinanceREST(url)
            self._kcache.clear()
            self.symbols.clear()
            self.last_universe = 0
            self.restart_stream()

    def ws_url(self) -> str:
        return config.TESTNET_WS_MARKET if self.base_url == config.TESTNET_REST else config.MAINNET_WS_MARKET

    async def start(self) -> None:
        await self.refresh_universe(force=True)
        self.restart_stream()

    def restart_stream(self) -> None:
        if self.sim:
            return
        if self.stream:
            self.stream.stop()
        if self._stream_task:
            self._stream_task.cancel()
        self.stream = MarketStream(self.ws_url(), ["!markPrice@arr@1s", "!forceOrder@arr"], self._on_ws)
        self._stream_task = asyncio.create_task(self.stream.run(self.log))

    def _on_ws(self, stream: str, data) -> None:
        if stream.startswith("!markPrice") and isinstance(data, list):
            for d in data:
                try:
                    self.marks[d["s"]] = (float(d["p"]), int(d["E"]))
                    if "r" in d and "T" in d:
                        self.funding[d["s"]] = (float(d["r"] or 0), int(d["T"] or 0))
                except (KeyError, TypeError, ValueError):
                    pass
        elif stream.startswith("!forceOrder"):
            self.liq.add(data)

    def ws_ok(self) -> bool:
        if self.sim:
            return True
        return bool(self.stream and self.stream.connected and time.time() - self.stream.last_msg < 15)

    async def poll_marks(self) -> None:
        """WebSocket yoksa / bozuksa REST ile mark fiyatı (yedek)."""
        if self.sim:
            now = self.clock.now_ms()
            for s in self.sim.series:
                self.marks[s] = (self.sim.mark(s), now)
            for ev in self.sim.liquidations():
                self.liq.add(ev)
            return
        try:
            data = await self.rest.premium_index()
            for d in data:
                self.marks[d["symbol"]] = (float(d["markPrice"]), self.clock.now_ms())
                self.funding[d["symbol"]] = (float(d.get("lastFundingRate") or 0), int(d.get("nextFundingTime") or 0))
            self.geo_blocked = False
        except BinanceError as e:
            self._handle_error(e)

    def _handle_error(self, e: Exception) -> None:
        from .binance_client import GeoBlockedError
        if isinstance(e, GeoBlockedError):
            if not self.geo_blocked:
                self.log("error", "Binance bu sunucunun konumunu engelliyor (HTTP 451). Render'da bölge Frankfurt veya "
                                  "Singapore olmalı. Frankfurt'ta da oluyorsa Singapore'da yeni servis aç.")
            self.geo_blocked = True
        self.last_error = str(e)

    def mark(self, symbol: str) -> float | None:
        m = self.marks.get(symbol)
        if not m:
            return None
        if self.clock.now_ms() - m[1] > 60_000:
            return None  # bayat fiyatla işlem yapma
        return m[0]

    async def refresh_universe(self, size: int = 30, force: bool = False) -> None:
        if not force and time.time() - self.last_universe < 3600 and self.universe:
            return
        try:
            if not self.symbols:
                self.symbols = await self.rest.exchange_info()
            tick = await self.rest.ticker_24h()
            ranked = sorted(
                (t for t in tick if t.get("symbol") in self.symbols
                 and self.symbols[t["symbol"]].base.upper() not in config.STABLE_BASES),
                key=lambda t: -float(t.get("quoteVolume") or 0))
            self.universe = [t["symbol"] for t in ranked[:size]]
            self.last_universe = time.time()
            self.geo_blocked = False
            self.log("info", f"Taranacak coinler güncellendi ({len(self.universe)}): " + ", ".join(self.universe[:12]) +
                     ("…" if len(self.universe) > 12 else ""))
        except BinanceError as e:
            self._handle_error(e)
            self.log("warn", f"Coin listesi alınamadı: {e}")

    async def candles(self, symbol: str, interval: str, limit: int = 300) -> Candles | None:
        """Sadece KAPANMIŞ mumlar (son oluşan mum atılır)."""
        ims = config.INTERVAL_MS[interval]
        now = self.clock.now_ms()
        bucket = now // ims
        key = (symbol, interval)
        hit = self._kcache.get(key)
        if hit and hit[0] == bucket:
            cols = hit[1]
        else:
            try:
                raw = await self.rest.klines(symbol, interval, limit=limit + 1)
            except BinanceError as e:
                self._handle_error(e)
                return None
            raw = [k for k in raw if int(k[6]) < now]
            cols = parse_klines(raw)
            self._kcache[key] = (bucket, cols)
        if len(cols["time"]) < 80:
            return None
        return Candles.from_cols(cols, ims)

    async def chart_candles(self, symbol: str, interval: str, start: int, end: int) -> list[dict]:
        ims = config.INTERVAL_MS[interval]
        start = max(start, end - 1400 * ims)
        try:
            raw = await self.rest.klines_range(symbol, interval, start, end)
        except BinanceError as e:
            self._handle_error(e)
            return []
        return [{"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5])} for k in raw]

    async def flow(self, symbol: str, sweep_minutes: int = 60) -> FlowSnapshot:
        fl = FlowSnapshot()
        longs, shorts = self.liq.window(symbol, sweep_minutes)
        bl, bs = self.liq.baseline(symbol, sweep_minutes)
        fl.liq_against_usd, fl.liq_for_usd = longs, shorts
        fl.liq_baseline_usd = max(bl, bs)
        f = self.funding.get(symbol)
        if f:
            fl.funding = f[0]
        try:
            hist = await self.rest.open_interest_hist(symbol, "15m", 12)
            fl.oi_change_pct = oi_change_pct(hist, 6)
        except BinanceError:
            pass
        mid = self.mark(symbol)
        if mid:
            try:
                fl.ob_imbalance = orderbook_imbalance(await self.rest.depth(symbol, 100), mid)
            except BinanceError:
                pass
            if coinglass.enabled():
                fl.cg_clusters_above, fl.cg_clusters_below = await coinglass.liq_clusters(symbol, mid)
        return fl
