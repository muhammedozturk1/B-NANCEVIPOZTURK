"""Exchange gateway: execution katmanının borsayla konuştuğu tek arayüz.

BinanceGateway gerçek Binance REST'i kullanır; testler aynı arayüzü uygulayan FakeExchange kullanır.
Koşullu emirler (STOP_MARKET / TAKE_PROFIT_MARKET) varsayılan olarak Algo Order API ile gönderilir.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from backend.smc.market_data.rest import CODE_ORDER_NOT_FOUND, BinanceAPIError, BinanceREST
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.risk.sizing import fmt_decimal, round_price, round_to_step

log = logging.getLogger(__name__)

ACTIVE_ORDER_STATUSES = {"NEW", "PARTIALLY_FILLED"}
ACTIVE_ALGO_STATUSES = {"NEW", "TRIGGERING"}


@dataclass
class AccountInfo:
    equity: float
    wallet_balance: float
    available_balance: float
    unrealized_pnl: float
    margin_used: float


@dataclass
class ExchangePosition:
    symbol: str
    qty: float  # işaretli: long > 0, short < 0
    entry_price: float
    mark_price: float = 0.0
    liq_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: int = 0
    margin_type: str = "isolated"


@dataclass
class OrderInfo:
    symbol: str
    client_id: str
    exchange_id: str | None
    is_algo: bool
    side: str
    type: str
    status: str
    qty: float
    filled_qty: float = 0.0
    avg_price: float = 0.0
    price: float = 0.0
    trigger_price: float = 0.0
    reduce_only: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status in (ACTIVE_ALGO_STATUSES if self.is_algo else ACTIVE_ORDER_STATUSES)


@dataclass
class FillInfo:
    trade_id: str
    order_id: str
    side: str
    price: float
    qty: float
    commission: float
    commission_asset: str
    realized_pnl: float
    maker: bool
    time: int


class ExchangeGateway(Protocol):
    async def account(self) -> AccountInfo: ...
    async def positions(self) -> list[ExchangePosition]: ...
    async def open_orders(self, symbol: str | None = None) -> list[OrderInfo]: ...
    async def ensure_symbol_setup(self, symbol: str, leverage: int) -> None: ...
    async def place_limit(self, symbol: str, side: str, qty: float, price: float, client_id: str,
                          f: SymbolFilters) -> OrderInfo: ...
    async def get_order(self, symbol: str, client_id: str, is_algo: bool = False) -> OrderInfo | None: ...
    async def cancel(self, symbol: str, client_id: str, is_algo: bool = False) -> None: ...
    async def place_stop(self, symbol: str, side: str, qty: float, trigger: float, client_id: str,
                         f: SymbolFilters) -> OrderInfo: ...
    async def place_take_profit(self, symbol: str, side: str, qty: float, trigger: float, client_id: str,
                                f: SymbolFilters) -> OrderInfo: ...
    async def market_reduce(self, symbol: str, side: str, qty: float, client_id: str,
                            f: SymbolFilters | None) -> OrderInfo: ...
    async def cancel_all(self, symbol: str) -> None: ...
    async def fills_since(self, symbol: str, start_ms: int) -> list[FillInfo]: ...
    async def funding_since(self, symbol: str, start_ms: int) -> float: ...


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class BinanceGateway:
    def __init__(self, rest: BinanceREST, *, use_algo: bool = True, working_type: str = "MARK_PRICE",
                 margin_type: str = "ISOLATED", attempts: int = 3):
        self.rest = rest
        self.use_algo = use_algo
        self.working_type = working_type
        self.margin_type = margin_type
        self.attempts = attempts
        self._configured: dict[str, int] = {}

    # ---------------------------------------------------------------- reads
    async def account(self) -> AccountInfo:
        a = await self.rest.account()
        return AccountInfo(equity=_f(a.get("totalMarginBalance")), wallet_balance=_f(a.get("totalWalletBalance")),
                           available_balance=_f(a.get("availableBalance")),
                           unrealized_pnl=_f(a.get("totalUnrealizedProfit")),
                           margin_used=_f(a.get("totalInitialMargin")))

    async def positions(self) -> list[ExchangePosition]:
        out = []
        for p in await self.rest.position_risk():
            q = _f(p.get("positionAmt"))
            if q == 0:
                continue
            out.append(ExchangePosition(p["symbol"], q, _f(p.get("entryPrice")), _f(p.get("markPrice")),
                                        _f(p.get("liquidationPrice")), _f(p.get("unRealizedProfit")),
                                        int(_f(p.get("leverage"), 0)), str(p.get("marginType", "")).lower()))
        return out

    @staticmethod
    def _norm_order(o: dict) -> OrderInfo:
        return OrderInfo(o["symbol"], o.get("clientOrderId", ""), str(o.get("orderId")), False, o.get("side", ""),
                         o.get("type", o.get("origType", "")), o.get("status", ""), _f(o.get("origQty")),
                         _f(o.get("executedQty")), _f(o.get("avgPrice")), _f(o.get("price")),
                         _f(o.get("stopPrice")), bool(o.get("reduceOnly")), o)

    @staticmethod
    def _norm_algo(o: dict) -> OrderInfo:
        return OrderInfo(o.get("symbol", ""), o.get("clientAlgoId", ""), str(o.get("algoId")), True,
                         o.get("side", ""), o.get("orderType", o.get("type", "")),
                         o.get("algoStatus", o.get("status", "")), _f(o.get("quantity", o.get("origQty"))),
                         _f(o.get("executedQty")), _f(o.get("actualPrice", o.get("avgPrice"))), _f(o.get("price")),
                         _f(o.get("triggerPrice", o.get("stopPrice"))), bool(o.get("reduceOnly")), o)

    async def open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        orders = [self._norm_order(o) for o in await self.rest.open_orders(symbol)]
        if self.use_algo:
            orders += [self._norm_algo(o) for o in await self.rest.open_algo_orders(symbol)]
        return orders

    async def get_order(self, symbol: str, client_id: str, is_algo: bool = False) -> OrderInfo | None:
        try:
            if is_algo and self.use_algo:
                return self._norm_algo(await self.rest.query_algo_order(client_id))
            return self._norm_order(await self.rest.query_order(symbol, client_id))
        except BinanceAPIError as e:
            if e.code in CODE_ORDER_NOT_FOUND or e.status == 400:
                return None
            raise

    # -------------------------------------------------------------- setup
    async def ensure_symbol_setup(self, symbol: str, leverage: int) -> None:
        if self._configured.get(symbol) == leverage:
            return
        await self.rest.set_margin_type(symbol, self.margin_type)
        await self.rest.set_leverage(symbol, leverage)
        self._configured[symbol] = leverage

    # ------------------------------------------------------------- orders
    async def _idempotent(self, symbol: str, client_id: str, is_algo: bool, send) -> OrderInfo:
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                raw = await send()
                return self._norm_algo(raw) if is_algo else self._norm_order(raw)
            except BinanceAPIError as e:
                last = e
                if not (e.retryable or e.status == 0):
                    raise
                # Durum bilinmiyor: emir yerleşmiş mi? (duplicate order koruması)
                await asyncio.sleep(0.5 * (attempt + 1))
                existing = await self.get_order(symbol, client_id, is_algo)
                if existing is not None:
                    return existing
        assert last is not None
        raise last

    async def place_limit(self, symbol, side, qty, price, client_id, f) -> OrderInfo:
        px = round_price(price, f.tick_size, "down" if side == "BUY" else "up")
        q = round_to_step(qty, f.step_size, "down")
        return await self._idempotent(symbol, client_id, False, lambda: self.rest.new_order(
            symbol=symbol, side=side, type="LIMIT", timeInForce="GTC", quantity=fmt_decimal(q, f.step_size),
            price=fmt_decimal(px, f.tick_size), newClientOrderId=client_id, newOrderRespType="RESULT"))

    async def _conditional(self, symbol, side, qty, trigger, client_id, f, order_type) -> OrderInfo:
        q = round_to_step(qty, f.market_step_size, "down")
        trig = fmt_decimal(trigger, f.tick_size)
        if self.use_algo:
            return await self._idempotent(symbol, client_id, True, lambda: self.rest.new_algo_order(
                symbol=symbol, side=side, type=order_type, quantity=fmt_decimal(q, f.market_step_size),
                triggerPrice=trig, reduceOnly="true", workingType=self.working_type, priceProtect="true",
                clientAlgoId=client_id))
        return await self._idempotent(symbol, client_id, False, lambda: self.rest.new_order(
            symbol=symbol, side=side, type=order_type, quantity=fmt_decimal(q, f.market_step_size), stopPrice=trig,
            reduceOnly="true", workingType=self.working_type, priceProtect="true", newClientOrderId=client_id))

    async def place_stop(self, symbol, side, qty, trigger, client_id, f) -> OrderInfo:
        return await self._conditional(symbol, side, qty, trigger, client_id, f, "STOP_MARKET")

    async def place_take_profit(self, symbol, side, qty, trigger, client_id, f) -> OrderInfo:
        return await self._conditional(symbol, side, qty, trigger, client_id, f, "TAKE_PROFIT_MARKET")

    async def market_reduce(self, symbol, side, qty, client_id, f) -> OrderInfo:
        q = fmt_decimal(round_to_step(qty, f.market_step_size, "down"), f.market_step_size) if f else repr(qty)
        return await self._idempotent(symbol, client_id, False, lambda: self.rest.new_order(
            symbol=symbol, side=side, type="MARKET", quantity=q, reduceOnly="true", newClientOrderId=client_id,
            newOrderRespType="RESULT"))

    async def cancel(self, symbol: str, client_id: str, is_algo: bool = False) -> None:
        try:
            if is_algo and self.use_algo:
                await self.rest.cancel_algo_order(client_id)
            else:
                await self.rest.cancel_order(symbol, client_id)
        except BinanceAPIError as e:
            if e.code in CODE_ORDER_NOT_FOUND or e.status == 400:
                return
            raise

    async def cancel_all(self, symbol: str) -> None:
        try:
            await self.rest.cancel_all_orders(symbol)
        except BinanceAPIError as e:
            log.warning("cancel_all_orders %s: %s", symbol, e)
        if self.use_algo:
            try:
                await self.rest.cancel_all_algo_orders(symbol)
            except BinanceAPIError as e:
                log.warning("cancel_all_algo_orders %s: %s", symbol, e)

    # -------------------------------------------------------------- history
    async def fills_since(self, symbol: str, start_ms: int) -> list[FillInfo]:
        rows = await self.rest.user_trades(symbol, start_time=start_ms)
        return [FillInfo(str(r["id"]), str(r["orderId"]), r["side"], _f(r["price"]), _f(r["qty"]),
                         _f(r.get("commission")), r.get("commissionAsset", "USDT"), _f(r.get("realizedPnl")),
                         bool(r.get("maker")), int(r["time"])) for r in rows]

    async def funding_since(self, symbol: str, start_ms: int) -> float:
        rows = await self.rest.income(symbol=symbol, income_type="FUNDING_FEE", start_time=start_ms)
        return sum(_f(r.get("income")) for r in rows)
