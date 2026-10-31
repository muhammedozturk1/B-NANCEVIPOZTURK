"""Gateway arayüzünü uygulayan bellek içi sahte borsa (unit testler için)."""
from __future__ import annotations

import time

from backend.smc.execution.gateway import AccountInfo, ExchangePosition, FillInfo, OrderInfo
from backend.smc.market_data.rest import BinanceAPIError


class FakeExchange:
    use_algo = True

    def __init__(self, fill_ratio: float = 1.0, fail_stop: bool = False):
        self.fill_ratio = fill_ratio
        self.fail_stop = fail_stop
        self.positions_: dict[str, ExchangePosition] = {}
        self.orders: dict[str, OrderInfo] = {}
        self.fills: list[tuple[str, FillInfo]] = []
        self.setup_calls: list[tuple[str, int]] = []
        self.equity = 100.0
        self._tid = 0

    # ------------------------------------------------------------ helpers
    def _fill(self, symbol: str, side: str, qty: float, price: float, order_id: str) -> None:
        self._tid += 1
        pos = self.positions_.get(symbol)
        signed = qty if side == "BUY" else -qty
        realized = 0.0
        if pos is None:
            self.positions_[symbol] = ExchangePosition(symbol, signed, price)
        else:
            new_qty = round(pos.qty + signed, 10)
            if (pos.qty > 0) != (signed > 0):  # azaltma
                realized = (price - pos.entry_price) * (1 if pos.qty > 0 else -1) * min(abs(signed), abs(pos.qty))
            if new_qty == 0:
                del self.positions_[symbol]
            else:
                pos.qty = new_qty
        self.fills.append((symbol, FillInfo(str(self._tid), order_id, side, price, qty, price * qty * 0.0005, "USDT",
                                            realized, False, int(time.time() * 1000))))

    def trigger(self, client_id: str, price: float | None = None) -> None:
        """Koşullu emrin tetiklenmesini simüle eder."""
        o = self.orders[client_id]
        pos = self.positions_.get(o.symbol)
        qty = min(o.qty, abs(pos.qty)) if pos else 0.0
        if qty > 0:
            self._fill(o.symbol, o.side, qty, price or o.trigger_price, o.exchange_id)
        o.status = "FINISHED"
        o.filled_qty = qty

    def open_position(self, symbol: str, qty: float, price: float) -> None:
        self.positions_[symbol] = ExchangePosition(symbol, qty, price)

    # ---------------------------------------------------------- protocol
    async def account(self) -> AccountInfo:
        return AccountInfo(self.equity, self.equity, self.equity, 0.0, 0.0)

    async def positions(self) -> list[ExchangePosition]:
        return [p for p in self.positions_.values() if p.qty != 0]

    async def open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        return [o for o in self.orders.values() if o.active and (symbol is None or o.symbol == symbol)]

    async def ensure_symbol_setup(self, symbol: str, leverage: int) -> None:
        self.setup_calls.append((symbol, leverage))

    async def place_limit(self, symbol, side, qty, price, client_id, f) -> OrderInfo:
        filled = round(qty * self.fill_ratio, 3)
        status = "FILLED" if filled >= qty else ("PARTIALLY_FILLED" if filled > 0 else "NEW")
        o = OrderInfo(symbol, client_id, str(len(self.orders) + 1), False, side, "LIMIT", status, qty, filled,
                      price if filled else 0.0, price)
        self.orders[client_id] = o
        if filled:
            self._fill(symbol, side, filled, price, o.exchange_id)
        return o

    async def get_order(self, symbol, client_id, is_algo=False):
        return self.orders.get(client_id)

    async def cancel(self, symbol, client_id, is_algo=False) -> None:
        o = self.orders.get(client_id)
        if o and o.active:
            o.status = "CANCELED"

    async def place_stop(self, symbol, side, qty, trigger, client_id, f) -> OrderInfo:
        if self.fail_stop:
            raise BinanceAPIError(400, -2021, "Order would immediately trigger.")
        o = OrderInfo(symbol, client_id, f"a{len(self.orders) + 1}", True, side, "STOP_MARKET", "NEW", qty,
                      trigger_price=trigger, reduce_only=True)
        self.orders[client_id] = o
        return o

    async def place_take_profit(self, symbol, side, qty, trigger, client_id, f) -> OrderInfo:
        o = OrderInfo(symbol, client_id, f"a{len(self.orders) + 1}", True, side, "TAKE_PROFIT_MARKET", "NEW", qty,
                      trigger_price=trigger, reduce_only=True)
        self.orders[client_id] = o
        return o

    async def market_reduce(self, symbol, side, qty, client_id, f) -> OrderInfo:
        pos = self.positions_.get(symbol)
        q = min(qty, abs(pos.qty)) if pos else 0.0
        o = OrderInfo(symbol, client_id, str(len(self.orders) + 1), False, side, "MARKET", "FILLED", qty, q,
                      pos.entry_price if pos else 0.0, reduce_only=True)
        self.orders[client_id] = o
        if q:
            self._fill(symbol, side, q, pos.entry_price, o.exchange_id)
        return o

    async def cancel_all(self, symbol: str) -> None:
        for o in self.orders.values():
            if o.symbol == symbol and o.active:
                o.status = "CANCELED"

    async def fills_since(self, symbol: str, start_ms: int) -> list[FillInfo]:
        return [f for s, f in self.fills if s == symbol]

    async def funding_since(self, symbol: str, start_ms: int) -> float:
        return 0.0
