"""
FAZ 1 - TEST SCRIPTİ

Bu aşamada henüz gerçek/testnet işlem AÇILMIYOR. Bu script sadece:
  1) Binance Testnet bağlantısının çalıştığını
  2) Veritabanının kurulduğunu
  3) EMA ve VWAP göstergelerinin doğru hesaplandığını
doğrulamak için var. İşlem açma mantığı Faz 2'de (engines/) eklenecek.
"""
import logging
from backend import config
from backend.exchange.binance_client import BinanceClient
from backend.db.database import init_db
from backend.strategies import ema, vwap

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")


def run_phase1_check():
    logger.info("=== FAZ 1 BAĞLANTI TESTİ BAŞLIYOR ===")

    # 1) Veritabanı
    init_db()
    logger.info("✅ Veritabanı tabloları hazır.")

    # 2) Binance bağlantısı
    client = BinanceClient()
    try:
        balance = client.get_usdt_balance()
        logger.info(f"✅ Binance bağlantısı OK. Testnet USDT bakiyesi: {balance}")
    except Exception as e:
        logger.error(f"❌ Binance bağlantı hatası: {e}")
        logger.error("Kontrol et: .env dosyasındaki API key/secret doğru mu? Testnet API key mi?")
        return

    # 3) Göstergeler
    for symbol in config.SYMBOLS:
        try:
            candles = client.fetch_ohlcv(symbol, timeframe="15m", limit=100)
            ema_signal = ema.get_signal(candles)
            vwap_signal = vwap.get_signal(candles)
            logger.info(f"📊 {symbol} | EMA: {ema_signal['direction']} | VWAP: {vwap_signal['direction']}")
        except Exception as e:
            logger.error(f"❌ {symbol} veri çekilemedi: {e}")

    logger.info("=== FAZ 1 TESTİ TAMAMLANDI ===")


if __name__ == "__main__":
    run_phase1_check()
