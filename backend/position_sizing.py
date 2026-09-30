"""
Pozisyon büyüklüğü, kaldıraç ve SL/TP hesabı.

Kurallar:
  - Risk her zaman kasanın sabit yüzdesidir (varsayılan %1). Eski koddaki
    "min 5$/10$ risk" kaldırıldı: 100$ kasada bu %5-10 risk demekti.
  - Kaldıraç sabit 20x değil: marj tavanına sığacak EN DÜŞÜK kaldıraç seçilir,
    ve likidasyon fiyatı stoptan en az 3 kat uzakta kalacak şekilde sınırlanır.
"""
import math
from backend import config


def stop_distance(engine: str, price: float, atr_value: float) -> float:
    p = config.EXIT_PARAMS[engine]
    return max(atr_value * p["sl_atr_mult"], price * p["min_stop_pct"])


def sl_tp_prices(engine: str, side: str, entry_price: float, dist: float) -> dict:
    d = 1 if side == "buy" else -1
    tp_r = config.EXIT_PARAMS[engine]["tp_r"]
    return {
        "stop_loss": entry_price - d * dist,
        "take_profit": entry_price + d * dist * tp_r,
    }


def calculate_position(engine: str, price: float, dist: float, balance: float) -> dict:
    risk_usd = balance * config.RISK_PERCENT_PER_TRADE[engine]
    amount = risk_usd / dist
    notional = amount * price

    max_margin = balance * config.MAX_MARGIN_PERCENT_PER_TRADE
    stop_pct = dist / price
    lev_liq_cap = max(1, math.floor(1 / (config.LIQUIDATION_SAFETY_MULT * stop_pct)))
    lev_cap = min(config.MAX_LEVERAGE, lev_liq_cap)

    leverage = max(1, min(lev_cap, math.ceil(notional / max_margin)))
    margin = notional / leverage
    capped = False
    if margin > max_margin:  # kaldıraç tavanına rağmen sığmıyor -> küçült
        scale = max_margin / margin
        amount *= scale
        notional *= scale
        risk_usd *= scale
        margin = max_margin
        capped = True

    return {
        "risk_usd": round(risk_usd, 4),
        "amount": amount,
        "notional_usd": round(notional, 2),
        "required_margin_usd": round(margin, 2),
        "leverage": int(leverage),
        "capped_by_margin_limit": capped,
    }


def check_margin_sufficient(required_margin: float, available_balance: float) -> bool:
    return required_margin <= available_balance * 0.95
