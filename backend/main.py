"""
ANA ÇALIŞMA DÖNGÜSÜ

  - Fonlama motoru (v3 ana strateji): saatte bir, her saatin 5. dakikası.
    Paper modunda API key GEREKMEZ, sadece Binance'in herkese açık verisini okur.
  - SMC Scalper (şartname v2.1, backend/smc): ayrı thread'de, kendi asyncio döngüsüyle.
    WebSocket ile 3m/5m/15m veri, native SL/TP, %5/%15/%10 risk motoru. SMC_ENABLED ile açılıp kapanır.
  - Yönlü motorlar (scalp/day/swing): varsayılan KAPALI. Açıksa kendi mumlarının
    kapanışına hizalı çalışır (5m motor: her 5 dakikanın 10. saniyesinde).

v3.1 düzeltmesi: Eskiden bot açılırken testnet'e bağlanıp bakiye okuyamazsa
TAMAMEN duruyordu. Oysa fonlama motoru testnet'e hiç ihtiyaç duymuyor. Artık
borsa (testnet/canlı) bağlantısı sadece yönlü motorlardan biri açıksa zorunlu.
"""
import logging
from apscheduler.schedulers.blocking import BlockingScheduler

from backend import config
from backend.db import status as bot_status
from backend.db.database import init_db
from backend.engines import scalp, day, swing
from backend.risk import position_monitor
from backend.funding import engine as funding_engine
from backend.telegram import notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")

ENGINES = {"scalp": scalp, "day": day, "swing": swing}


def run_engine_safely(engine_module, client):
    """Bir motorda hata olsa bile diğer motorları ve zamanlayıcıyı etkilememesi için sarmalayıcı."""
    try:
        engine_module.run(client)
        bot_status.mark_ok(engine_module.ENGINE_NAME)
    except Exception as e:
        logger.exception(f"{engine_module.ENGINE_NAME} motorunda beklenmeyen hata: {e}")
        bot_status.mark_error(engine_module.ENGINE_NAME, e)


def run_funding_safely():
    try:
        funding_engine.run_cycle()
    except Exception as e:
        logger.exception(f"[funding] döngü hatası: {e}")
        bot_status.mark_error("funding", e)


def run_position_monitor_safely(client):
    try:
        position_monitor.run_monitor_cycle(client)
    except Exception as e:
        logger.error(f"Pozisyon izleme döngüsünde hata: {e}")


def run_heartbeat_safely():
    try:
        bot_status.beat()
        notifier.send_heartbeat()
    except Exception as e:
        logger.error(f"Heartbeat gönderilirken hata: {e}")


def run_weekly_summary_safely():
    try:
        notifier.send_weekly_summary()
    except Exception as e:
        logger.error(f"Haftalık özet gönderilirken hata: {e}")


def directional_engines_enabled() -> list:
    return [name for name in ENGINES if config.ENGINE_ENABLED.get(name)]


def connect_exchange():
    """Testnet/canlı borsa bağlantısı. Başarısızsa None döner (bot çökmez)."""
    try:
        from backend.exchange.binance_client import BinanceClient
        client = BinanceClient()
        balance = client.get_usdt_balance()
        logger.info(f"✅ Binance bağlantısı OK. {'Testnet' if config.USE_TESTNET else 'CANLI'} USDT bakiyesi: {balance}")
        return client
    except Exception as e:
        logger.error(f"❌ Binance (emir) bağlantısı kurulamadı: {e}")
        bot_status.mark_error("exchange", e)
        return None


def startup_checks():
    """Dönüş: borsa istemcisi (veya None). Veritabanı yoksa exception fırlatır."""
    logger.info("=== BAŞLANGIÇ KONTROLLERİ ===")
    init_db()
    logger.info("✅ Veritabanı tabloları hazır.")

    client = None
    enabled = directional_engines_enabled()
    if enabled:
        client = connect_exchange()
        if client is None:
            logger.error(f"Yönlü motorlar ({', '.join(enabled)}) borsa bağlantısı olmadan çalışamaz, "
                         f"bu oturumda KAPALI. Fonlama motoru etkilenmez.")
        else:
            logger.info(f"Tarama modu: DİNAMİK - en aktif {config.TOP_SYMBOLS_COUNT} parite, "
                        f"min. hacim: {config.MIN_24H_VOLUME_USDT:,.0f}$")
            # Bağlantı koptuktan sonra yeniden başlarsak, borsa ile DB'yi senkronize et
            position_monitor.reconcile_positions_on_startup(client)
    else:
        logger.info("Yönlü motorlar kapalı -> borsa (testnet) bağlantısı gerekmiyor.")

    if not enabled and not config.FUNDING["enabled"]:
        logger.warning("⚠️ Hiçbir motor açık değil! (ENGINE_* ve FUNDING_ENABLED ayarlarını kontrol et)")

    bot_status.beat()
    logger.info("=== KONTROLLER TAMAMLANDI, BOT ÇALIŞMAYA BAŞLIYOR ===")
    notifier.send_heartbeat()
    return client


CRON_FOR_TF = {
    # Mum kapanışından 10 sn sonra çalış -> her mum TAM OLARAK bir kez değerlendirilir
    "5m": {"minute": "*/5", "second": 10},
    "15m": {"minute": "*/15", "second": 10},
    "1h": {"minute": 0, "second": 15},
}


def build_scheduler(client) -> BlockingScheduler:
    from backend.ai import model as ai_model
    scheduler = BlockingScheduler(timezone="UTC")

    for name, module in ENGINES.items():
        if not config.ENGINE_ENABLED.get(name):
            logger.info(f"[{name}] motor KAPALI (config.ENGINE_ENABLED).")
            continue
        if client is None:
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

    if client is not None:
        scheduler.add_job(run_position_monitor_safely, "interval",
                          seconds=config.POSITION_MONITOR_INTERVAL_SECONDS,
                          args=[client], id="position_monitor", max_instances=1, coalesce=True)
    # Dashboard'daki "bot çalışıyor" göstergesi bu kayda bakar (Telegram'dan bağımsız, 5 dk'da bir)
    scheduler.add_job(bot_status.beat, "interval", minutes=5, id="status_beat")
    scheduler.add_job(run_heartbeat_safely, "interval",
                      minutes=config.HEARTBEAT_INTERVAL_MINUTES, id="heartbeat")
    scheduler.add_job(run_weekly_summary_safely, "cron",
                      day_of_week=config.WEEKLY_SUMMARY_DAY_OF_WEEK,
                      hour=config.WEEKLY_SUMMARY_HOUR_UTC, id="weekly_summary")
    return scheduler


def start_smc_scalper():
    """Şartname v2.1 SMC scalper'ı ayrı thread'de başlatır (backend/smc). Hata olursa
    sadece loglanır; fonlama ve diğer motorlar etkilenmez."""
    try:
        from backend.smc import runner as smc_runner
        return smc_runner.start_in_thread()
    except Exception as e:
        logger.error(f"[smc] SMC scalper başlatılamadı: {e}")
        bot_status.mark_error("smc", e)
        return None


def main():
    client = startup_checks()
    start_smc_scalper()
    scheduler = build_scheduler(client)
    logger.info(f"Zamanlayıcı başlatıldı | AI modu: {config.AI_MODE} | işlem başı risk: "
                f"{config.RISK_PERCENT_PER_TRADE} | günlük zarar limiti: %{config.DAILY_MAX_LOSS_PERCENT*100:.0f}")
    scheduler.start()


if __name__ == "__main__":
    main()
