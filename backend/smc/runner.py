"""SMC Scalper'ı mevcut botun worker sürecinde AYRI BİR THREAD'de çalıştırır.

- Kendi asyncio event loop'u vardır; mevcut APScheduler döngüsünü (funding vb.) bloklamaz.
- SMC içinde beklenmeyen bir hata olursa thread yeniden başlatılır (30 sn → 10 dk backoff),
  mevcut botun motorları etkilenmez.
- API key yoksa veya SMC_ENABLED=false ise hiç başlatılmaz.
- Canlılık bilgisi mevcut botun `bot_status` tablosuna "smc" bileşeni olarak da yazılır.

Not: Aynı süreçte çalıştığı için süreç seviyesindeki bir çöküş (bellek yetersizliği, Render
restart) ikisini birlikte durdurur. Bu durumda Binance üzerindeki native SL/TP emirleri
pozisyonları korumaya devam eder ve SMC açılışta reconciliation yapar.
"""
from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger("smc.runner")
_stop = threading.Event()
_thread: threading.Thread | None = None


def _status(ok: bool, info=None) -> None:
    try:
        from backend.db import status as bot_status
        if ok:
            bot_status.mark_ok("smc", info)
        else:
            bot_status.mark_error("smc", info)
    except Exception:  # durum kaydı asla SMC'yi durdurmamalı
        pass


def _run_forever() -> None:
    from backend.smc.config import Settings
    from backend.smc.worker import TradingWorker

    backoff = 30.0
    while not _stop.is_set():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            worker = TradingWorker(Settings())
            worker.heartbeat_hook = lambda info: _status(True, info)
            loop.run_until_complete(worker.run())
            backoff = 30.0
        except Exception as exc:
            log.exception("SMC worker hatası, %.0f sn sonra yeniden başlatılacak", backoff)
            _status(False, f"SMC worker hatası: {exc!r}")
        finally:
            try:
                loop.close()
            except Exception:
                pass
        if _stop.wait(backoff):
            break
        backoff = min(backoff * 2, 600.0)


def start_in_thread() -> threading.Thread | None:
    """Mevcut botun main()'inden çağrılır. Başlatamazsa None döner, asla exception fırlatmaz."""
    global _thread
    try:
        from backend.smc.config import Settings
        s = Settings()
    except Exception as exc:
        log.error("SMC konfigürasyonu geçersiz, SMC başlatılmadı: %s", exc)
        _status(False, f"konfigürasyon hatası: {exc}")
        return None
    if not s.enabled:
        log.info("[smc] SMC_ENABLED=false -> SMC scalper kapalı.")
        return None
    if not s.binance_api_key.get_secret_value() or not s.binance_api_secret.get_secret_value():
        log.error("[smc] BINANCE_API_KEY / BINANCE_API_SECRET yok -> SMC scalper başlatılmadı.")
        _status(False, "Binance testnet API key eksik")
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_run_forever, name="smc-worker", daemon=True)
    _thread.start()
    log.info("[smc] SMC scalper ayrı thread'de başlatıldı (%s, %s).", s.trading_env, s.account_scope)
    return _thread


def stop() -> None:
    _stop.set()
