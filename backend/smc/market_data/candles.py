"""Kapanmış mum deposu. Strateji katmanına yalnızca kapanmış mumlar verilir."""
from __future__ import annotations

from collections import deque

from backend.smc.core.types import TIMEFRAME_MS, Candle


class CandleStore:
    def __init__(self, maxlen: int = 600):
        self.maxlen = maxlen
        self._data: dict[tuple[str, str], deque[Candle]] = {}
        self.last_update_ms: dict[tuple[str, str], int] = {}

    def _dq(self, symbol: str, tf: str) -> deque[Candle]:
        key = (symbol, tf)
        if key not in self._data:
            self._data[key] = deque(maxlen=self.maxlen)
        return self._data[key]

    def set_history(self, symbol: str, tf: str, candles: list[Candle], server_now_ms: int) -> None:
        dq = self._dq(symbol, tf)
        dq.clear()
        for c in candles:
            if c.close_time < server_now_ms:  # açık mumu at
                dq.append(c)
        if dq:
            self.last_update_ms[(symbol, tf)] = dq[-1].close_time

    def add_closed(self, symbol: str, tf: str, candle: Candle) -> bool:
        """Kapanmış mumu ekler. Yeni mumsa True döner."""
        dq = self._dq(symbol, tf)
        if dq and candle.open_time <= dq[-1].open_time:
            if candle.open_time == dq[-1].open_time:
                dq[-1] = candle
            return False
        if dq and candle.open_time - dq[-1].open_time > TIMEFRAME_MS[tf]:
            # boşluk var: backfill gerekecek (gap'i işaretle)
            self.last_update_ms[(symbol, tf)] = -1
        else:
            self.last_update_ms[(symbol, tf)] = candle.close_time
        dq.append(candle)
        return True

    def has_gap(self, symbol: str, tf: str) -> bool:
        return self.last_update_ms.get((symbol, tf)) == -1

    def get(self, symbol: str, tf: str, n: int | None = None) -> list[Candle]:
        dq = self._data.get((symbol, tf))
        if not dq:
            return []
        items = list(dq)
        return items[-n:] if n else items

    def closes(self, symbol: str, tf: str, n: int) -> list[float]:
        return [c.close for c in self.get(symbol, tf, n)]

    def is_stale(self, symbol: str, tf: str, now_ms: int, factor: float = 2.5) -> bool:
        last = self.last_update_ms.get((symbol, tf))
        if not last or last < 0:
            return True
        return now_ms - last > TIMEFRAME_MS[tf] * factor

    def symbols(self) -> set[str]:
        return {s for s, _ in self._data}
