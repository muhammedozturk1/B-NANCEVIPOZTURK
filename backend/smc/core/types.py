from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def entry_order_side(self) -> str:
        return "BUY" if self is Side.LONG else "SELL"

    @property
    def exit_order_side(self) -> str:
        return "SELL" if self is Side.LONG else "BUY"

    @property
    def direction(self) -> str:
        return "bullish" if self is Side.LONG else "bearish"

    @classmethod
    def from_direction(cls, direction: str) -> "Side":
        return cls.LONG if direction == "bullish" else cls.SHORT


@dataclass(frozen=True, slots=True)
class Candle:
    """Kapanmış mum. Açık mum strateji katmanına asla verilmez."""

    open_time: int  # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int  # ms
    taker_buy_volume: float = 0.0
    trades: int = 0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open

    @property
    def delta(self) -> float:
        """Synthetic delta = agresif alış hacmi - agresif satış hacmi.

        Binance kline 'taker buy base volume' alanı agresif (taker) alış hacmidir;
        kalan hacim agresif satıştır. Bu gerçek exchange footprint değildir.
        """
        return 2.0 * self.taker_buy_volume - self.volume

    @property
    def close_location(self) -> float:
        """0 = low'da kapanış, 1 = high'da kapanış."""
        if self.range <= 0:
            return 0.5
        return (self.close - self.low) / self.range

    @classmethod
    def from_rest(cls, row: list) -> "Candle":
        return cls(
            open_time=int(row[0]), open=float(row[1]), high=float(row[2]), low=float(row[3]),
            close=float(row[4]), volume=float(row[5]), close_time=int(row[6]),
            trades=int(row[8]), taker_buy_volume=float(row[9]),
        )

    @classmethod
    def from_ws(cls, k: dict) -> "Candle":
        return cls(
            open_time=int(k["t"]), open=float(k["o"]), high=float(k["h"]), low=float(k["l"]),
            close=float(k["c"]), volume=float(k["v"]), close_time=int(k["T"]),
            trades=int(k.get("n", 0)), taker_buy_volume=float(k.get("V", 0.0)),
        )


TIMEFRAME_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """SQLite naive datetime döndürür; hepsini UTC-aware yapar."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
