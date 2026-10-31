"""TP / Breakeven / R hesapları (live ve backtest aynı fonksiyonları kullanır)."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from backend.smc.config import Settings
from backend.smc.core.types import Side
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.risk.sizing import round_price, round_to_step, round_tp_price


@dataclass
class TPLevel:
    name: str
    r_multiple: float
    price: float
    qty: float
    filled: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def tp_prices(side: Side, entry: float, stop: float, cfg: Settings) -> list[tuple[str, float, float]]:
    r = abs(entry - stop)
    s = side.sign
    return [("TP1", cfg.tp1_r, entry + s * cfg.tp1_r * r), ("TP2", cfg.tp2_r, entry + s * cfg.tp2_r * r),
            ("TP3", cfg.tp3_r, entry + s * cfg.tp3_r * r)]


def plan_take_profits(side: Side, entry: float, stop: float, qty: float, filters: SymbolFilters,
                      cfg: Settings) -> list[TPLevel]:
    """TP1 %50, TP2 %25, TP3 %25 (gerçek dolan miktar üzerinden).

    Bir dilim minimum miktarın altında kalırsa bir sonraki dilimle birleştirilir; TP3 her zaman
    kalan miktarın tamamıdır, böylece toplam = qty.
    """
    prices = tp_prices(side, entry, stop, cfg)
    pcts = [cfg.tp1_pct, cfg.tp2_pct, cfg.tp3_pct]
    min_q = float(filters.min_qty)
    raw = [round_to_step(qty * p, filters.step_size, "down") for p in pcts[:2]]
    levels: list[TPLevel] = []
    carry = 0.0
    allocated = 0.0
    for (name, rm, px), q in zip(prices[:2], raw):
        q = round_to_step(q + carry, filters.step_size, "down")
        if q < min_q:
            carry = q
            continue
        carry = 0.0
        levels.append(TPLevel(name, rm, round_tp_price(px, side, filters.tick_size), q))
        allocated += q
    last_q = round_to_step(qty - allocated, filters.step_size, "down")
    name, rm, px = prices[2]
    if last_q >= min_q or not levels:
        levels.append(TPLevel(name, rm, round_tp_price(px, side, filters.tick_size), last_q))
    else:  # kalan çok küçükse son seviyeye ekle
        levels[-1].qty = round_to_step(levels[-1].qty + last_q, filters.step_size, "down")
    return levels


def breakeven_price(side: Side, entry: float, cfg: Settings, tick=None) -> float:
    """Entry + (giriş + çıkış komisyonu + slippage) buffer. Short için ters yönde."""
    buf = entry * (2 * cfg.taker_fee + cfg.slippage_pct)
    px = entry + side.sign * buf
    if tick is not None:
        px = round_price(px, tick, "up" if side is Side.LONG else "down")
    return px


def stop_after_tp(side: Side, entry: float, tp_levels: list[TPLevel], filled_names: set[str], current_stop: float,
                  cfg: Settings, tick=None) -> float:
    """TP1 sonrası BE, TP2 sonrası (config açıksa) TP1 seviyesi. SL asla geri gitmez."""
    new = current_stop
    if "TP1" in filled_names:
        new = _better(side, new, breakeven_price(side, entry, cfg, tick))
    if "TP2" in filled_names and cfg.move_sl_to_tp1_after_tp2:
        tp1 = next((t.price for t in tp_levels if t.name == "TP1"), None)
        if tp1 is not None:
            new = _better(side, new, tp1)
    return new


def _better(side: Side, a: float, b: float) -> float:
    return max(a, b) if side is Side.LONG else min(a, b)


def r_multiple(net_pnl: float, initial_risk: float) -> float:
    return net_pnl / initial_risk if initial_risk > 0 else 0.0


def planned_rr(side: Side, entry: float, stop: float, levels: list[TPLevel], cfg: Settings) -> float:
    """Ağırlıklı, komisyon/slippage sonrası planlanan RR."""
    total_q = sum(l.qty for l in levels)
    if total_q <= 0:
        return 0.0
    loss_u = abs(entry - stop) + entry * cfg.taker_fee + stop * cfg.taker_fee + (entry + stop) * cfg.slippage_pct
    gain = 0.0
    for l in levels:
        gain += l.qty * (abs(l.price - entry) - entry * cfg.taker_fee - l.price * cfg.taker_fee
                         - entry * cfg.slippage_pct)
    return gain / (total_q * loss_u)


def open_risk(side: Side, entry: float, stop: float, remaining_qty: float, cfg: Settings) -> float:
    """Pozisyonun şu anki SL'ye göre açık riski (BE sonrası ~0)."""
    adverse = (entry - stop) * side.sign  # SL entry'nin zararlı tarafındaysa > 0
    if adverse <= 0 or remaining_qty <= 0:
        return 0.0
    exit_cost = stop * (cfg.taker_fee + cfg.slippage_pct)
    return remaining_qty * (adverse + exit_cost)
