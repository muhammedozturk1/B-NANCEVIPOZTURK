"""Geliştirme/test için sentetik piyasa (SMCBOT_OFFLINE_SIM=true).

Binance'e erişmeden botun tamamını (tarama, giriş, stop/TP, panel) uçtan uca
çalıştırmayı sağlar. Zaman SMCBOT_SIM_SPEED kat hızlı akar. Gerçek kullanımda kapalıdır.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal

import numpy as np

from .binance_client import SymbolInfo
from .config import INTERVAL_MS

MIN = 60_000
SYMBOLS = {"BTCUSDT": 68000, "ETHUSDT": 3400, "SOLUSDT": 160, "BNBUSDT": 600, "XRPUSDT": 0.62,
           "DOGEUSDT": 0.16, "ADAUSDT": 0.45, "AVAXUSDT": 35, "LINKUSDT": 15, "SUIUSDT": 1.8,
           "LTCUSDT": 85, "DOTUSDT": 6.5}


class SimClock:
    def __init__(self, speed: float):
        self.speed = speed
        self.real0 = time.time()
        # sim saati: 6 gün geçmiş veri + şimdi
        self.sim0 = (int(time.time() * 1000) // MIN) * MIN

    def now_ms(self) -> int:
        return int(self.sim0 + (time.time() - self.real0) * 1000 * self.speed)


class _Series:
    def __init__(self, symbol: str, base: float, start: int, seed: int):
        self.symbol = symbol
        self.start = start
        self.rng = np.random.default_rng(seed)
        self.vol = 0.0009 * (1.0 + (seed % 5) * 0.15)
        self.o = np.empty(0); self.h = np.empty(0); self.l = np.empty(0); self.c = np.empty(0)
        self.v = np.empty(0); self.tb = np.empty(0)
        self.last = base
        self.drift = 0.0
        self.extend(16 * 1440)

    def extend(self, n: int) -> None:
        rng, vol = self.rng, self.vol
        o = np.empty(n); h = np.empty(n); lo = np.empty(n); c = np.empty(n); v = np.empty(n); tb = np.empty(n)
        p = self.last
        for i in range(n):
            if rng.random() < 1 / 240:
                self.drift = rng.normal(0, vol * 0.25)
            r = rng.normal(self.drift, vol)
            op = p
            cl = op * np.exp(r)
            hi = max(op, cl) * (1 + abs(rng.normal(0, vol * 0.6)))
            lw = min(op, cl) * (1 - abs(rng.normal(0, vol * 0.6)))
            vv = float(np.exp(rng.normal(0, 0.5))) * (1 + 40 * abs(r))
            if rng.random() < 0.004:  # stop avı iğnesi
                k = rng.uniform(3, 7) * vol
                if rng.random() < 0.5:
                    lw = min(lw, op * (1 - k)); vv *= 4
                else:
                    hi = max(hi, op * (1 + k)); vv *= 4
            share = 0.5 + 0.35 * np.tanh(r / vol) + rng.normal(0, 0.05)
            o[i], h[i], lo[i], c[i], v[i] = op, hi, lw, cl, vv * 100
            tb[i] = v[i] * min(0.95, max(0.05, share))
            p = cl
        self.last = p
        self.o = np.concatenate([self.o, o]); self.h = np.concatenate([self.h, h])
        self.l = np.concatenate([self.l, lo]); self.c = np.concatenate([self.c, c])
        self.v = np.concatenate([self.v, v]); self.tb = np.concatenate([self.tb, tb])

    def ensure(self, ts: int) -> None:
        need = (ts - self.start) // MIN + 2
        while len(self.c) < need:
            self.extend(1440)

    def price_at(self, ts: int) -> float:
        self.ensure(ts)
        i = int((ts - self.start) // MIN)
        frac = ((ts - self.start) % MIN) / MIN
        o, c = self.o[i], self.c[i]
        # dakika içinde tepeye/dibe uğrayan basit yol
        if frac < 0.33:
            return float(o + (self.l[i] - o) * frac / 0.33) if c > o else float(o + (self.h[i] - o) * frac / 0.33)
        if frac < 0.66:
            a = self.l[i] if c > o else self.h[i]
            b = self.h[i] if c > o else self.l[i]
            return float(a + (b - a) * (frac - 0.33) / 0.33)
        b = self.h[i] if c > o else self.l[i]
        return float(b + (c - b) * (frac - 0.66) / 0.34)


class SimREST:
    def __init__(self) -> None:
        speed = float(os.environ.get("SMCBOT_SIM_SPEED", "60"))
        self.clock = SimClock(speed)
        start = self.clock.sim0 - 14 * 86_400_000
        self.series = {s: _Series(s, p, start, i + 7) for i, (s, p) in enumerate(SYMBOLS.items())}
        self.used_weight = 0
        self._liq_cursor = self.clock.now_ms()

    async def close(self) -> None:
        return None

    async def sync_time(self) -> None:
        return None

    async def exchange_info(self) -> dict[str, SymbolInfo]:
        out = {}
        for s, p in SYMBOLS.items():
            tick = Decimal("0.1") if p > 1000 else Decimal("0.01") if p > 10 else Decimal("0.0001") if p > 0.1 else Decimal("0.000001")
            step = Decimal("0.001") if p > 1000 else Decimal("0.01") if p > 10 else Decimal("1")
            out[s] = SymbolInfo(s, s[:-4], tick, step, step, step, step, Decimal("1000000"), Decimal("5"))
        return out

    async def ticker_24h(self) -> list[dict]:
        return [{"symbol": s, "quoteVolume": str(1e9 / (i + 1))} for i, s in enumerate(SYMBOLS)]

    def _klines(self, symbol: str, interval: str, start: int, end: int) -> list[list]:
        ser = self.series[symbol]
        ims = INTERVAL_MS[interval]
        now = self.clock.now_ms()
        end = min(end, now)
        ser.ensure(end)
        out = []
        b = max((start // ims) * ims, ((ser.start + ims - 1) // ims) * ims)
        while b <= end:
            i0 = int((b - ser.start) // MIN)
            i1 = int((min(b + ims, now) - ser.start) // MIN)
            i1 = max(i1, i0 + 1)
            o = ser.o[i0]; h = ser.h[i0:i1].max(); lo = ser.l[i0:i1].min(); c = ser.c[i1 - 1]
            if b + ims > now:  # oluşan mum
                c = ser.price_at(now)
                h, lo = max(h, c), min(lo, c)
            v = ser.v[i0:i1].sum(); tb = ser.tb[i0:i1].sum()
            out.append([b, f"{o}", f"{h}", f"{lo}", f"{c}", f"{v}", b + ims - 1, f"{v * c}", 100, f"{tb}", f"{tb * c}", "0"])
            b += ims
        return out

    async def klines(self, symbol: str, interval: str, limit: int = 300, start: int | None = None, end: int | None = None) -> list[list]:
        ims = INTERVAL_MS[interval]
        now = self.clock.now_ms()
        end = end or now
        start = start or (end - (limit - 1) * ims)
        return self._klines(symbol, interval, start, end)[-limit:]

    async def klines_range(self, symbol: str, interval: str, start: int, end: int) -> list[list]:
        return self._klines(symbol, interval, start, end)

    def mark(self, symbol: str) -> float:
        return self.series[symbol].price_at(self.clock.now_ms())

    async def premium_index(self) -> list[dict]:
        now = self.clock.now_ms()
        nxt = (now // 28_800_000 + 1) * 28_800_000
        return [{"symbol": s, "markPrice": str(self.mark(s)), "lastFundingRate": "0.0001", "nextFundingTime": nxt}
                for s in SYMBOLS]

    async def open_interest_hist(self, symbol: str, period: str = "15m", limit: int = 30) -> list[dict]:
        rng = np.random.default_rng(int(self.clock.now_ms() // 900_000))
        base = 1e8
        return [{"sumOpenInterestValue": str(base * (1 + rng.normal(0, 0.01)))} for _ in range(limit)]

    async def depth(self, symbol: str, limit: int = 100) -> dict:
        m = self.mark(symbol)
        rng = np.random.default_rng(int(self.clock.now_ms() // 60_000))
        bids = [[str(m * (1 - 0.0005 * (i + 1))), str(abs(rng.normal(10, 5)))] for i in range(limit)]
        asks = [[str(m * (1 + 0.0005 * (i + 1))), str(abs(rng.normal(10, 5)))] for i in range(limit)]
        return {"bids": bids, "asks": asks}

    def liquidations(self) -> list[dict]:
        """Son çağrıdan bu yana iğne mumlarından sahte likidasyon olayları."""
        now = self.clock.now_ms()
        out = []
        for s, ser in self.series.items():
            ser.ensure(now)
            i0 = int((self._liq_cursor - ser.start) // MIN)
            i1 = int((now - ser.start) // MIN)
            for i in range(max(0, i0), i1):
                rng_ = ser.h[i] - ser.l[i]
                body = abs(ser.c[i] - ser.o[i])
                if rng_ > 0 and body / rng_ < 0.3 and rng_ / ser.c[i] > ser.vol * 3:
                    down = (min(ser.o[i], ser.c[i]) - ser.l[i]) > (ser.h[i] - max(ser.o[i], ser.c[i]))
                    out.append({"o": {"s": s, "S": "SELL" if down else "BUY", "ap": str(ser.c[i]),
                                      "z": str(ser.v[i] * 0.2), "T": ser.start + i * MIN}})
        self._liq_cursor = now
        return out
