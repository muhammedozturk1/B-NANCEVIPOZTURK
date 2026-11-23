"""SMC Bot ayarları.

Bot, dashboard servisinin (crypto-bot-dashboard) içinde çalışır ve mevcut botla aynı
Postgres veritabanını kullanır (tablolar `smcbot_` önekli). Panel girişi mevcut panel
girişidir (DASHBOARD_USERNAME / DASHBOARD_PASSWORD_HASH).

Gizli bilgiler (API anahtarları) SADECE ortam değişkenlerinden okunur.
"""
from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    v = _env(name)
    return default if v == "" else v.lower() in ("1", "true", "yes", "evet", "on")


# Botu tamamen kapatmak için SMCBOT_ENABLED=false
ENABLED = _flag("SMCBOT_ENABLED", True)

# Testnet: kendi anahtarı yoksa mevcut botun BINANCE_API_KEY'i kullanılır (USE_TESTNET=true iken testnet anahtarıdır)
_USE_TESTNET = _flag("USE_TESTNET", True)
TESTNET_API_KEY = _env("SMCBOT_TESTNET_API_KEY") or (_env("BINANCE_API_KEY") if _USE_TESTNET else "")
TESTNET_API_SECRET = _env("SMCBOT_TESTNET_API_SECRET") or (_env("BINANCE_API_SECRET") if _USE_TESTNET else "")
# Gerçek hesap: SADECE bu değişkenler (BINANCE_API_KEY testnet anahtarı olduğu için asla kullanılmaz)
LIVE_API_KEY = _env("SMCBOT_LIVE_API_KEY")
LIVE_API_SECRET = _env("SMCBOT_LIVE_API_SECRET")

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

COINGLASS_API_KEY = _env("COINGLASS_API_KEY")

# Gerçek moda geçiş kilidini kaldırmak için bilinçli olarak "true" yazılmalı (önerilmez).
ALLOW_LIVE_WITHOUT_GATE = _flag("SMCBOT_ALLOW_LIVE_WITHOUT_GATE", False)

# Geliştirme/test: Binance yerine sentetik piyasa
OFFLINE_SIM = _flag("SMCBOT_OFFLINE_SIM", False)

# ---- Binance adresleri (Kasım 2026 itibarıyla) ----
MAINNET_REST = "https://fapi.binance.com"
MAINNET_WS_MARKET = "wss://fstream.binance.com/market/stream"
TESTNET_REST = _env("SMCBOT_TESTNET_REST_URL", "https://demo-fapi.binance.com")
TESTNET_WS_MARKET = _env("SMCBOT_TESTNET_WS_URL", "wss://demo-fstream.binance.com/market/stream")

STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "PYUSD", "EUR", "XUSD", "USD1"}

# ---- Panelden değiştirilebilen ayarlar (veritabanında saklanır) ----
DEFAULT_SETTINGS: dict = {
    "mode": "paper",                 # paper | testnet | live
    "running": False,                # bot yeni işlem arıyor mu
    "paper_start_balance": 1000.0,

    # Risk
    "risk_per_trade_pct": 1.0,       # stop olursa kaybedilecek bakiye yüzdesi (test edilen: %1)
    "leverage": 10,
    "max_open_positions": 3,
    "max_same_direction": 2,         # aynı yönde (hepsi long gibi) en fazla
    "daily_loss_limit_pct": 10.0,    # gün içi zarar bu yüzdeye ulaşınca yeni işlem yok
    "max_consecutive_losses": 3,     # art arda bu kadar stop -> soğuma
    "cooldown_hours": 4,
    "max_drawdown_pct": 30.0,        # zirveden bu kadar düşüş -> bot tamamen durur
    "min_stop_pct": 0.30,            # çok dar stoplar gürültüde patlar (SMC)
    "max_stop_pct": 4.00,            # SMC için üst sınır (trendde kaldıraç otomatik düşer)

    # Strateji seçimi: trend (4s trend takibi, varsayılan) | smc (likidite avı + FVG, eski)
    "strategy": "trend",
    "trend_n": 20,                   # kırılım kanalı: önceki N adet 4s mum
    "trend_stop_atr": 3.0,           # başlangıç stopu = K x ATR(14, 4s)
    "trend_exit_n": 10,              # iz süren stop: son M adet 4s mumun dibi/tepesi
    "fng_filter": True,              # aşırı açgözlülükte long, aşırı korkuda short açma
    "trend_max_new_per_bar": 1,      # aynı 4s kapanışında en fazla bu kadar yeni giriş (aynı anda kırılan coinler
                                     # çoğu zaman tek bir piyasa hareketidir; 2023-2026 testinde 1 en iyisi)
    "telegram_daily_summary": True,  # her gün 23:05'te (TR) Telegram'a günlük özet

    # Trend çıkış yönetimi (exits.py; canlı ve geçmiş test aynı motoru kullanır). 1R = |giriş - ilk stop|, sabit.
    # Varsayılanlar 2020-2026 / 20 coin karşılaştırmasına göre seçildi (docs/TREND_CIKIS_RAPORU.md):
    # sıkı kâr kilitleri büyük trendleri erken kesip toplam getiriyi düşürdü; +2R'de başabaş neredeyse bedava.
    "lock_tiers": "2.0:0",           # biçim "tetik_R:kilit_R" virgülle, örn. "1.0:0, 1.5:0.5, 2.0:1.0"; boş = kapalı
    "lock_fee_cover": True,          # kilit seviyesine komisyon+kaymayı ekle (0R gerçekten ~0 olsun)
    "partial_tp_tiers": "",          # biçim "tetik_R:oran" virgülle, örn. "2.0:0.33, 3.0:0.25" (ilk miktarın oranı); boş = kapalı
    "exit_trigger_source": "candle_close",  # kilit/kısmi TP tetiği: mark_price | last_price | candle_close
    "manage_tf": "4h",               # trailing güncelleme + candle_close tetik sıklığı: 1h | 4h
    "trail_mode": "swing",           # off | swing (son M mumun dibi) | atr (kapanış - K×ATR) | both (en koruyucusu)
    "trail_activate_r": -1.0,        # trailing bu R görülünce başlar; -1 = hemen (eski davranış)
    "trail_atr_mult": 3.0,

    # Rejim filtreleri (her biri ayrı açılır/kapanır)
    "fng_missing": "allow",          # F&G alınamazsa: allow | skip | neutral_fallback (50 say)
    "fng_long_max": 75,              # F&G >= bu değer: long açma (aşırı açgözlülük)
    "fng_short_min": 25,             # F&G <= bu değer: short açma (aşırı korku)
    "fng_long_min": 0,               # F&G < bu değer: long açma (0 = kapalı)
    "fng_short_max": 100,            # F&G > bu değer: short açma (100 = kapalı)
    "filter_volume": False, "min_volume_ratio": 1.2,          # kırılım mumu hacmi / 20 mum ortalaması
    "filter_body": False, "min_body_ratio": 0.5,              # gövde / (tepe - dip)
    "filter_close_strength": False, "min_close_strength": 0.6,  # kapanışın mum içindeki yeri (long: 1 = tepede)
    "filter_correlation": False, "max_correlation": 0.8, "corr_lookback": 60,  # aynı yöndeki açık pozisyonlarla

    # SMC stratejisi
    "entry_mode": "ce",              # ce | edge | market
    "min_score": 65,
    "min_rr": 2.0,
    "tp1_r": 1.0,                    # 1R'de yarısını kapat, stopu girişe çek
    "tp1_close_pct": 50,
    "setup_expiry_bars": 8,          # giriş fiyatına bu kadar mumda gelmezse iptal
    "universe_size": 30,             # hacme göre ilk N coin taranır
    "ltf": "15m",
    "htf": "1h",
    "require_liquidity_target": True,
    "notify_setups": False,          # Telegram'a "kurulum bulundu" da gönderilsin mi

    # Maliyet varsayımları (demo için)
    "taker_fee_pct": 0.05,
    "slippage_pct": 0.02,

    # Gerçek moda geçiş kilidi
    "live_unlock_min_trades": 100,
    "live_unlock_min_pf": 1.2,

    # Geçmiş test
    "backtest_min_score": 55,
}

# Panelden girilen değerlerin geçerli aralıkları (yanlış yazılan 109 gibi değerler sınıra çekilir)
SETTING_LIMITS: dict = {
    "paper_start_balance": (10.0, 10_000_000.0),
    "risk_per_trade_pct": (0.1, 10.0),
    "leverage": (1, 50),
    "max_open_positions": (1, 10),
    "max_same_direction": (1, 10),       # ayrıca en fazla max_open_positions
    "daily_loss_limit_pct": (1.0, 50.0),
    "max_consecutive_losses": (1, 20),
    "cooldown_hours": (0, 72),
    "max_drawdown_pct": (5.0, 90.0),
    "trend_n": (5, 100),
    "trend_stop_atr": (0.5, 10.0),
    "trend_exit_n": (2, 60),
    "trend_max_new_per_bar": (0, 10),     # 0 = sınırsız
    "trail_activate_r": (-1.0, 10.0),
    "trail_atr_mult": (0.5, 10.0),
    "fng_long_max": (1, 101), "fng_short_min": (-1, 99), "fng_long_min": (0, 100), "fng_short_max": (0, 100),
    "min_volume_ratio": (0.0, 10.0), "min_body_ratio": (0.0, 1.0), "min_close_strength": (0.0, 1.0),
    "max_correlation": (0.0, 1.0), "corr_lookback": (10, 500),
    "universe_size": (1, 60),
    "taker_fee_pct": (0.0, 1.0),
    "slippage_pct": (0.0, 1.0),
    "live_unlock_min_trades": (1, 10_000),
}

SETTING_CHOICES: dict = {
    "exit_trigger_source": ("mark_price", "last_price", "candle_close"),
    "manage_tf": ("1h", "4h"),
    "trail_mode": ("off", "swing", "atr", "both"),
    "fng_missing": ("allow", "skip", "neutral_fallback"),
}

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}
