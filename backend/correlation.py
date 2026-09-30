"""
Portföy risk limitleri.

Eski sürüm sadece BTC/ETH'yi korelasyonlu sayıyordu. Oysa en hacimli 15
altcoinin hepsi BTC ile birlikte hareket eder: 5 farklı coinde long açmak
aslında tek büyük BTC long'udur. Artık:
  1) Aynı yöndeki TÜM açık işlemlerin toplam riski <= MAX_SAME_DIRECTION_RISK_PERCENT
  2) Tüm açık işlemlerin toplam riski <= MAX_TOTAL_OPEN_RISK_PERCENT
"""
import logging
from backend import config
from backend.db.database import get_session
from backend.db.models import Trade, TradeStatus

logger = logging.getLogger("correlation")


def _open_risk(side: str = None) -> float:
    session = get_session()
    try:
        q = session.query(Trade).filter(Trade.status == TradeStatus.OPEN)
        if side:
            q = q.filter(Trade.side == side)
        return sum(t.risk_usd for t in q.all())
    finally:
        session.close()


def total_open_risk_ok(balance: float, extra_risk: float = 0.0) -> bool:
    return _open_risk() + extra_risk <= balance * config.MAX_TOTAL_OPEN_RISK_PERCENT


def check_correlation_limit(symbol: str, side: str, new_risk_usd: float, current_balance: float) -> bool:
    same_dir = _open_risk(side) + new_risk_usd
    if same_dir > current_balance * config.MAX_SAME_DIRECTION_RISK_PERCENT:
        logger.info(f"🔗 {symbol}: {side} yönünde toplam risk {same_dir:.2f}$ tavanı aşıyor.")
        return False
    if not total_open_risk_ok(current_balance, new_risk_usd):
        logger.info(f"🔗 {symbol}: toplam açık risk tavanı aşılıyor.")
        return False
    return True
