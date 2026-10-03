"""
ANA ÇALIŞMA DÖNGÜSÜ (v2)

Motorlar artık sabit aralıkla değil, kendi mumlarının KAPANIŞINA hizalı
çalışır (5m motor: her 5 dakikanın 10. saniyesinde).

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
from backend.funding import engine as funding_engine
from backend.telegram import notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")


def run_engine_safely(engine_module, client):
    """Bir motorda hata olsa bile diğer motorları ve zamanlayıcıyı etkilememesi için sarmalayıcı."""
    try:
        engine_module.run(client)
    except Exception as e:
        logger.error(f"{engine_module.ENGINE_NAME} motorunda beklenmeyen hata: {e}")


def run_funding_safely():
    try:
        funding_engine.run_cycle()
    except Exception as e:
        logger.exception(f"[funding] döngü hatası: {e}")


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


CRON_FOR_TF = {
    # Mum kapanışından 10 sn sonra çalış -> her mum TAM OLARAK bir kez değerlendirilir
    "5m": {"minute": "*/5", "second": 10},
    "15m": {"minute": "*/15", "second": 10},
    "1h": {"minute": 0, "second": 15},
}


def main():
    client = BinanceClient()

    if not startup_checks(client):
        logger.error("Başlangıç kontrolleri başarısız, bot durduruluyor.")
        return

    from backend.ai import model as ai_model
    engines = {"scalp": scalp, "day": day, "swing": swing}
    scheduler = BlockingScheduler(timezone="UTC")

    for name, module in engines.items():
        if not config.ENGINE_ENABLED.get(name):
            logger.info(f"[{name}] motor KAPALI (config.ENGINE_ENABLED).")
            continue
        ready, msg = ai_model.engine_ready(name)
        logger.info(f"[{name}] {config.ENGINE_TIMEFRAMES[name]} | AI: {msg}")
        tf = config.ENGINE_TIMEFRAMES[name]["entry"]
        scheduler.add_job(run_engine_safely, "cron", args=[module, client], id=f"{name}_engine",
                          max_instances=1, coalesce=True, **CRON_FOR_TF[tf])

    if config.FUNDING["enabled"]:
        logger.info(f"[funding] Fonlama motoru AKTİF | mod: {config.FUNDING['mode']} | "
                    f"en fazla {config.FUNDING['max_positions']} pozisyon | her saatin 5. dakikası")
        scheduler.add_job(run_funding_safely, "cron", minute=5, second=0, id="funding_engine",
                          max_instances=1, coalesce=True)
        scheduler.add_job(run_funding_safely, "date", id="funding_first_run")  # açılışta hemen bir kez

    scheduler.add_job(run_position_monitor_safely, "interval",
                      seconds=config.POSITION_MONITOR_INTERVAL_SECONDS,
                      args=[client], id="position_monitor", max_instances=1, coalesce=True)
    scheduler.add_job(run_heartbeat_safely, "interval",
                      minutes=config.HEARTBEAT_INTERVAL_MINUTES, id="heartbeat")
    scheduler.add_job(run_weekly_summary_safely, "cron",
                      day_of_week=config.WEEKLY_SUMMARY_DAY_OF_WEEK,
                      hour=config.WEEKLY_SUMMARY_HOUR_UTC, id="weekly_summary")

    logger.info(f"Zamanlayıcı başlatıldı | AI modu: {config.AI_MODE} | işlem başı risk: "
                f"{config.RISK_PERCENT_PER_TRADE} | günlük zarar limiti: %{config.DAILY_MAX_LOSS_PERCENT*100:.0f}")
    scheduler.start()


if __name__ == "__main__":
    main()
