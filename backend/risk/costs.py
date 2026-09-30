"""Komisyon + kayma dahil NET kâr/zarar hesabı (eski kod komisyonu hiç düşmüyordu)."""
from backend import config


def estimate_fees_usd(entry_price: float, exit_price: float, amount: float) -> float:
    return (entry_price + exit_price) * abs(amount) * config.COST_PER_SIDE


def estimate_net_pnl(side: str, entry_price: float, exit_price: float, amount: float) -> float:
    direction = 1 if side == "buy" else -1
    gross = (exit_price - entry_price) * amount * direction
    return gross - estimate_fees_usd(entry_price, exit_price, amount)
