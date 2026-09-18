"""
Telegram Bildirim Modülü.

FAZ 2 NOTU: Bu dosya şimdilik sadece LOG'a yazıyor (placeholder).
Gerçek Telegram mesaj gönderimi (açılış bildirimi + reply-thread TP/SL
güncellemeleri + heartbeat + haftalık özet) Faz 4'te eklenecek.
Bu sayede motorlar şimdiden "bildirim gönderiyormuş gibi" çalışabiliyor,
Faz 4'te bu dosyanın içini dolduracağız, motor kodlarına dokunmayacağız.
"""
import logging

logger = logging.getLogger("notifier")


def send_trade_opened(trade):
    logger.info(f"📨 [TELEGRAM-PLACEHOLDER] Yeni işlem bildirimi gönderilecek: "
                f"{trade.engine} | {trade.symbol} | {trade.side} @ {trade.entry_price}")


def send_trade_update(trade, event: str):
    """event örn: 'TP1', 'TP2', 'SL', 'BE'"""
    logger.info(f"📨 [TELEGRAM-PLACEHOLDER] İşlem güncelleme bildirimi: "
                f"{trade.engine} | {trade.symbol} | {event}")
