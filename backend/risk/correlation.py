"""
Korelasyon Filtresi.

3 motor aynı anda BTC ve ETH gibi korelasyonu yüksek paritelerde aynı yöne
işlem açarsa, bu aslında tek büyük pozisyon riskine dönüşür. Bu modül,
yeni bir işlem açılmadan önce, korelasyonlu grup içindeki AYNI YÖNDEKİ
toplam risk miktarını kontrol eder.
"""
import logging
from backend import config
from backend.db.database import get_session
from backend.db.models import Trade, TradeStatus

logger = logging.getLogger("correlation")


def get_correlated_group(symbol: str):
    for group in config.CORRELATED_GROUPS:
        if symbol in group:
            return group
    return None


def get_same_direction_exposure(group: set, side: str) -> float:
    """Verilen korelasyon grubundaki, verilen yöndeki tüm AÇIK işlemlerin toplam risk_usd'sini döner."""
    session = get_session()
    try:
        trades = session.query(Trade).filter(
            Trade.symbol.in_(list(group)),
            Trade.side == side,
            Trade.status == TradeStatus.OPEN,
        ).all()
        return sum(t.risk_usd for t in trades)
    finally:
        session.close()


def check_correlation_limit(symbol: str, side: str, new_risk_usd: float, current_balance: float) -> bool:
    """
    True dönerse işlem açılabilir. False dönerse korelasyon limiti aşılıyor demektir, işlem açılmamalı.
    """
    group = get_correlated_group(symbol)
    if not group:
        return True  # bu sembol için tanımlı bir korelasyon grubu yok, engel yok

    existing_exposure = get_same_direction_exposure(group, side)
    total_exposure = existing_exposure + new_risk_usd
    max_allowed = current_balance * config.MAX_SAME_DIRECTION_EXPOSURE_PERCENT

    if total_exposure > max_allowed:
        logger.info(f"🔗 Korelasyon limiti aşılıyor: {group} grubunda {side} yönünde "
                    f"toplam risk {total_exposure:.2f}$ > izin verilen {max_allowed:.2f}$")
        return False
    return True
