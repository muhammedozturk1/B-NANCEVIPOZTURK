"""
FAZ 2 - ANA ÇALIŞMA DÖNGÜSÜ

Faz 1'deki "bir kere çalışıp kapanan" test scriptinin yerini alıyor.
Artık bot sürekli çalışır ve 3 motoru kendi zaman dilimlerine göre
periyodik olarak tetikler:
  - Scalp: her 1 dakikada bir kontrol
  - Day:   her 15 dakikada bir kontrol
  - Swing: her 4 saatte bir kontrol

Render worker artık bir kere çalışıp kapanmayacak, sürekli ayakta kalacak.
"""
import logging
from apscheduler.schedulers.blocking import BlockingScheduler

from backend import config
from backend.db.database import init_db
from backend.exchange.binance_client import BinanceClient
from backend.engines import scalp, day, swing

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")


def run_engine_safely(engine_module, client):
    """Bir motorda hata olsa bile diğer motorları ve zamanlayıcıyı etkilememesi için sarmalayıcı."""
    try:
        engine_module.run(client)
    except Exception as e:
        logger.error(f"{engine_module.ENGINE_NAME} motorunda beklenmeyen hata: {e}")


def startup_checks(client) -> bool:
    """Bot başlamadan önce temel bağlantıları doğrular (Faz 1'deki testin aynısı)."""
    logger.info("=== BAŞLANGIÇ KONTROLLERİ ===")
    init_db()
    logger.info("✅ Veritabanı tabloları hazır.")

    try:
        balance = client.get_usdt_balance()
        logger.info(f"✅ Binance bağlantısı OK. Testnet USDT bakiyesi: {balance}")
    except Exception as e:
        logger.error(f"❌ Binance bağlantı hatası: {e}")
        return False

    logger.info(f"İzlenen pariteler: {config.SYMBOLS}")
    logger.info("=== KONTROLLER TAMAMLANDI, BOT ÇALIŞMAYA BAŞLIYOR ===")
    return True


def main():
    client = BinanceClient()

    if not startup_checks(client):
        logger.error("Başlangıç kontrolleri başarısız, bot durduruluyor.")
        return

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(run_engine_safely, "interval", minutes=1,
                       args=[scalp, client], id="scalp_engine")
    scheduler.add_job(run_engine_safely, "interval", minutes=15,
                       args=[day, client], id="day_engine")
    scheduler.add_job(run_engine_safely, "interval", hours=4,
                       args=[swing, client], id="swing_engine")

    logger.info("Zamanlayıcı başlatıldı: Scalp(1dk) / Day(15dk) / Swing(4sa)")

    # İlk çalıştırmada hemen bir kez tetikle, sonra periyodik devam etsin
    run_engine_safely(scalp, client)
    run_engine_safely(day, client)
    run_engine_safely(swing, client)

    scheduler.start()


if __name__ == "__main__":
    main()
