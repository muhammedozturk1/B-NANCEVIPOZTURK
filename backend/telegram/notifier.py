"""
FAZ 4 - GERÇEK TELEGRAM BİLDİRİM MODÜLÜ

Faz 2/3'te bu dosya sadece log'a yazıyordu (placeholder). Artık gerçek
Telegram mesajları gönderiyor:

  1) İşlem açıldığında bildirim (mesaj ID kaydedilir)
  2) TP1/TP2/SL/BE/erken kapama güncellemeleri, açılış mesajına REPLY olarak
  3) Heartbeat: bot her N dakikada "hâlâ çalışıyorum" mesajı gönderir
  4) Haftalık performans özeti: motor bazlı işlem sayısı, kazanma oranı, PnL

NOT: python-telegram-bot yerine basit HTTP istekleri (requests) kullanılıyor -
bu ihtiyacımız için yeterli ve ağır bir kütüphaneye gerek yok.
"""
import logging
import requests
from datetime import datetime, timedelta
from backend import config
from backend.db import repository
from backend.db.database import get_session
from backend.db.models import Trade, TradeStatus

logger = logging.getLogger("notifier")


def _send_raw(text: str, reply_to_message_id: int = None) -> dict:
    """Telegram API'sine ham istek gönderir. Başarısız olursa None döner, botu düşürmez."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram token/chat_id tanımlı değil, mesaj gönderilmedi.")
        return None

    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        response = requests.post(f"{config.TELEGRAM_API_BASE}/sendMessage", json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Telegram mesajı gönderilemedi: {e}")
        return None


ENGINE_LABELS = {"scalp": "⚡ Scalp", "day": "📆 Day", "swing": "🌊 Swing"}
SIDE_LABELS = {"buy": "🟢 LONG", "sell": "🔴 SHORT"}


def send_trade_opened(trade: Trade):
    text = (
        f"<b>{ENGINE_LABELS.get(trade.engine, trade.engine)} — Yeni İşlem</b>\n"
        f"{SIDE_LABELS.get(trade.side, trade.side)} {trade.symbol}\n\n"
        f"Giriş: <code>{trade.entry_price:.4f}</code>\n"
        f"Kaldıraç: {trade.leverage}x\n"
        f"Stop: <code>{trade.stop_loss:.4f}</code>\n"
        f"TP1: <code>{trade.take_profit_1:.4f}</code>\n"
        f"TP2: <code>{trade.take_profit_2:.4f}</code>\n"
        f"Risk: {trade.risk_usd:.2f}$\n"
        f"Confluence skoru: {trade.confluence_score} ({trade.strategies_used})"
    )
    result = _send_raw(text)
    if result and result.get("ok"):
        message_id = result["result"]["message_id"]
        repository.set_telegram_message_id(trade.id, message_id)
    else:
        logger.warning(f"[{trade.engine}] {trade.symbol}: açılış bildirimi gönderilemedi veya message_id alınamadı.")


EVENT_LABELS = {
    TradeStatus.CLOSED_TP1.value: "✅ TP1 vuruldu",
    TradeStatus.CLOSED_TP2.value: "✅✅ TP2 vuruldu",
    TradeStatus.CLOSED_SL.value: "🛑 Stop-loss oldu",
    TradeStatus.CLOSED_BE.value: "⚖️ Breakeven'da kapandı",
    TradeStatus.CLOSED_MANUAL.value: "⚠️ Erken kapatıldı (momentum kaybı)",
}


def send_trade_update(trade: Trade, event: str):
    label = EVENT_LABELS.get(event, event)
    pnl_line = f"\nPnL: {trade.pnl_usd:.2f}$" if trade.pnl_usd is not None else ""
    text = f"<b>{trade.symbol}</b> ({ENGINE_LABELS.get(trade.engine, trade.engine)})\n{label}{pnl_line}"

    reply_id = trade.telegram_message_id  # açılış mesajına reply olarak gider
    result = _send_raw(text, reply_to_message_id=reply_id)

    if not result or not result.get("ok"):
        # reply_to_message_id geçersizse (örn. mesaj silinmişse) Telegram hata verir,
        # bu durumda normal (reply'sız) mesaj olarak tekrar dene
        logger.warning(f"[{trade.engine}] {trade.symbol}: reply gönderilemedi, normal mesaj deneniyor.")
        _send_raw(text)


def send_heartbeat():
    """Bot her HEARTBEAT_INTERVAL_MINUTES'te bir bu mesajı gönderir.
    Mesajlar kesilirse bot çökmüş demektir - harici izleme olmadan en basit yöntem."""
    open_trades = repository.get_open_trades()
    virtual_balance = repository.get_virtual_balance()
    text = (
        f"💓 Bot çalışıyor.\n"
        f"Sanal kasa: {virtual_balance:.2f}$\n"
        f"Açık işlem sayısı: {len(open_trades)}"
    )
    _send_raw(text)


def send_weekly_summary():
    """Son 7 günün motor bazlı performans özetini gönderir."""
    since = datetime.utcnow() - timedelta(days=7)
    trades = repository.get_trades_since(since)
    closed_trades = [t for t in trades if t.status != TradeStatus.OPEN]

    if not closed_trades:
        _send_raw("📊 <b>Haftalık Özet</b>\nBu hafta kapanan işlem olmadı.")
        return

    lines = ["📊 <b>Haftalık Performans Özeti</b>\n"]
    for engine in ("scalp", "day", "swing"):
        engine_trades = [t for t in closed_trades if t.engine == engine]
        if not engine_trades:
            continue
        wins = [t for t in engine_trades if (t.pnl_usd or 0) > 0]
        total_pnl = sum(t.pnl_usd or 0 for t in engine_trades)
        win_rate = (len(wins) / len(engine_trades)) * 100

        lines.append(
            f"{ENGINE_LABELS.get(engine, engine)}: {len(engine_trades)} işlem | "
            f"Kazanma oranı: %{win_rate:.0f} | PnL: {total_pnl:.2f}$"
        )

    total_pnl_all = sum(t.pnl_usd or 0 for t in closed_trades)
    virtual_balance = repository.get_virtual_balance()
    lines.append(f"\n<b>Toplam PnL: {total_pnl_all:.2f}$</b>")
    lines.append(f"<b>Güncel sanal kasa: {virtual_balance:.2f}$</b>")

    _send_raw("\n".join(lines))
