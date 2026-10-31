"""Risk tabanlı position sizing.

KESİN RİSK FORMÜLÜ:
Current Equity → Risk = Equity × risk% → Entry/SL → Stop Distance → Position Notional →
Fees + Slippage → Max Notional → Leverage (≤10x ve ≤ exchange max) → Available Margin →
Liquidation Buffer → ALLOW / REJECT

Kaldıraç pozisyon boyutunu BELİRLEMEZ; yalnızca gerekli marjini belirler.
Pozisyon boyutu SL'ye ulaşıldığında kaybedilecek tutar (komisyon + slippage dahil)
risk bütçesini aşmayacak şekilde hesaplanır.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal

from backend.smc.config import Settings
from backend.smc.core.types import Side
from backend.smc.market_data.universe import SymbolFilters


# ------------------------------------------------------------------ rounding
def round_to_step(value: float, step: Decimal, mode: str = "down") -> float:
    """Miktarı step'e yuvarlar (varsayılan aşağı — risk asla aşılmaz)."""
    d = Decimal(str(value))
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "trunc": ROUND_DOWN}[mode]
    q = (d / step).to_integral_value(rounding=rounding) * step
    return float(q.quantize(step.normalize()) if step.normalize().as_tuple().exponent < 0 else q)


def round_price(price: float, tick: Decimal, mode: str = "nearest") -> float:
    d = Decimal(str(price))
    if mode == "nearest":
        q = (d / tick).quantize(Decimal(1)) * tick
    else:
        q = (d / tick).to_integral_value(rounding=ROUND_FLOOR if mode == "down" else ROUND_CEILING) * tick
    exp = tick.normalize().as_tuple().exponent
    return float(q.quantize(tick.normalize())) if isinstance(exp, int) and exp < 0 else float(q)


def round_stop_price(price: float, side: Side, tick: Decimal) -> float:
    """SL'yi entry'den UZAĞA yuvarla (stop mesafesi küçülmesin, risk artmasın)."""
    return round_price(price, tick, "down" if side is Side.LONG else "up")


def round_tp_price(price: float, side: Side, tick: Decimal) -> float:
    """TP'yi entry'ye DOĞRU yuvarla (muhafazakâr)."""
    return round_price(price, tick, "down" if side is Side.LONG else "up")


def fmt_decimal(value: float, step: Decimal) -> str:
    """Binance'e gönderilecek string (bilimsel gösterim yok)."""
    exp = step.normalize().as_tuple().exponent
    places = -exp if isinstance(exp, int) and exp < 0 else 0
    return f"{value:.{places}f}"


# -------------------------------------------------------------- liquidation
def estimate_liquidation_price(side: Side, entry: float, qty: float, leverage: int, mmr: float,
                               maint_amount: float = 0.0) -> float:
    """ISOLATED, one-way için Binance likidasyon formülü (tek pozisyon, ilk bracket).

    LP = (WB + cum - side·qty·entry) / (qty·MMR - side·qty), WB = qty·entry/leverage
    """
    if qty <= 0:
        return 0.0
    wb = qty * entry / leverage
    s = side.sign
    return max(0.0, (wb + maint_amount - s * qty * entry) / (qty * mmr - s * qty))


def liquidation_buffer_ok(side: Side, entry: float, stop: float, liq: float, cfg: Settings) -> tuple[bool, float]:
    stop_dist = abs(entry - stop)
    required = max(cfg.liq_buffer_pct * entry, cfg.liq_buffer_stop_ratio * stop_dist)
    gap = (stop - liq) if side is Side.LONG else (liq - stop)
    return gap >= required, gap


# ------------------------------------------------------------------- sizing
@dataclass
class SizingResult:
    ok: bool
    reason: str = ""
    qty: float = 0.0
    notional: float = 0.0
    leverage: int = 0
    margin: float = 0.0
    risk_target: float = 0.0
    risk_actual: float = 0.0
    per_unit_loss: float = 0.0
    est_fees: float = 0.0
    est_slippage: float = 0.0
    liq_price: float = 0.0
    capped_by: str = ""


def per_unit_loss(side: Side, entry: float, stop: float, cfg: Settings) -> tuple[float, float, float]:
    """SL'de birim başına toplam kayıp = stop mesafesi + entry/exit komisyonu + tahmini slippage."""
    fees = entry * cfg.taker_fee + stop * cfg.taker_fee
    slip = entry * cfg.slippage_pct + stop * cfg.slippage_pct
    return abs(entry - stop) + fees + slip, fees, slip


def compute_position_size(*, side: Side, equity: float, available_balance: float, entry: float, stop: float,
                          filters: SymbolFilters, cfg: Settings, risk_factor: float = 1.0) -> SizingResult:
    if equity <= 0:
        return SizingResult(False, "non_positive_equity")
    if (side is Side.LONG and stop >= entry) or (side is Side.SHORT and stop <= entry):
        return SizingResult(False, "stop_on_wrong_side")
    risk_target = equity * cfg.risk_per_trade * max(0.0, min(1.0, risk_factor))
    pul, fees_u, slip_u = per_unit_loss(side, entry, stop, cfg)
    qty = risk_target / pul
    capped_by = "risk"

    # Max position notional (risk motorundan bağımsız 2. güvenlik katmanı)
    if qty * entry > cfg.max_position_notional:
        qty = cfg.max_position_notional / entry
        capped_by = "max_notional"

    # Kaldıraç sınırı: config (≤10x) ve borsanın sembol için izin verdiği maksimum
    lev_cap = max(1, min(cfg.max_leverage, filters.max_leverage))
    max_by_margin = available_balance * cfg.max_margin_usage * lev_cap / entry
    if qty > max_by_margin:
        qty = max_by_margin
        capped_by = "available_margin"

    qty = round_to_step(qty, filters.step_size, "down")
    qty = min(qty, float(filters.max_qty), float(filters.market_max_qty))
    qty = round_to_step(qty, filters.step_size, "down")
    if qty < float(filters.min_qty) or qty <= 0:
        return SizingResult(False, "qty_below_min", risk_target=risk_target, capped_by=capped_by)
    notional = qty * entry
    if notional < filters.min_notional:
        return SizingResult(False, "notional_below_min", qty=qty, notional=notional, risk_target=risk_target)

    # Liquidation buffer: en yüksek kaldıraçtan başlayıp buffer sağlanana kadar düşür
    for lev in range(lev_cap, 0, -1):
        margin = notional / lev
        if margin > available_balance * cfg.max_margin_usage:
            break
        liq = estimate_liquidation_price(side, entry, qty, lev, filters.maint_margin_rate, filters.maint_amount)
        ok, _gap = liquidation_buffer_ok(side, entry, stop, liq, cfg)
        if ok:
            risk_actual = qty * pul
            if risk_actual > risk_target * (1 + 1e-9):
                return SizingResult(False, "risk_exceeds_target")
            return SizingResult(True, "", qty, notional, lev, margin, risk_target, risk_actual, pul,
                                qty * fees_u, qty * slip_u, liq, capped_by)
    return SizingResult(False, "liquidation_buffer_insufficient", qty=qty, notional=notional,
                        risk_target=risk_target)
