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
from backend.risk import position_monitor
from backend.telegram import notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")


def run_engine_safely(engine_module, client):
    """Bir motorda hata olsa bile diğer motorları ve zamanlayıcıyı etkilememesi için sarmalayıcı."""
    try:
        engine_module.run(client)
    except Exception as e:
        logger.error(f"{engine_module.ENGINE_NAME} motorunda beklenmeyen hata: {e}")


def run_position_monitor_safely(client):
    try:
        position_monitor.run_monitor_cycle(client)
    except Exception as e:
        logger.error(f"Pozisyon izleme döngüsünde hata: {e}")


def run_heartbeat_safely():
    try:
        notifier.send_heartbeat()
    except Exception as e:
        logger.error(f"Heartbeat gönderilirken hata: {e}")


def run_weekly_summary_safely():
    try:
        notifier.send_weekly_summary()
    except Exception as e:
        logger.error(f"Haftalık özet gönderilirken hata: {e}")


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

    logger.info(f"Tarama modu: DİNAMİK (sabit liste yok) - en aktif {config.TOP_SYMBOLS_COUNT} "
                f"parite, min. hacim: {config.MIN_24H_VOLUME_USDT:,.0f}$")

    # FAZ 3: Bağlantı koptuktan sonra yeniden başlarsak, borsa ile DB'yi senkronize et
    position_monitor.reconcile_positions_on_startup(client)

    logger.info("=== KONTROLLER TAMAMLANDI, BOT ÇALIŞMAYA BAŞLIYOR ===")
    notifier.send_heartbeat()
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
    scheduler.add_job(run_position_monitor_safely, "interval",
                       seconds=config.POSITION_MONITOR_INTERVAL_SECONDS,
                       args=[client], id="position_monitor")
    scheduler.add_job(run_heartbeat_safely, "interval",
                       minutes=config.HEARTBEAT_INTERVAL_MINUTES, id="heartbeat")
    scheduler.add_job(run_weekly_summary_safely, "cron",
                       day_of_week=config.WEEKLY_SUMMARY_DAY_OF_WEEK,
                       hour=config.WEEKLY_SUMMARY_HOUR_UTC, id="weekly_summary")

    logger.info(f"Zamanlayıcı başlatıldı: Scalp(1dk) / Day(15dk) / Swing(4sa) / "
                f"Pozisyon İzleme({config.POSITION_MONITOR_INTERVAL_SECONDS}sn) / "
                f"Heartbeat({config.HEARTBEAT_INTERVAL_MINUTES}dk) / Haftalık Özet(Pzt {config.WEEKLY_SUMMARY_HOUR_UTC}:00 UTC)")

    # İlk çalıştırmada hemen bir kez tetikle, sonra periyodik devam etsin
    run_engine_safely(scalp, client)
    run_engine_safely(day, client)
    run_engine_safely(swing, client)

    scheduler.start()


if __name__ == "__main__":
    main()
