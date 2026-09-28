"""
Merkezi konfigürasyon dosyası.
Tüm hassas bilgiler (.env) dosyasından okunur, kod içine ASLA API key yazılmaz.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


# ----------------------------------------------------------------------
# BORSA AYARLARI
# ----------------------------------------------------------------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

# ----------------------------------------------------------------------
# TELEGRAM AYARLARI
# ----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ----------------------------------------------------------------------
# VERİTABANI
# ----------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./crypto_bot.db")

# ----------------------------------------------------------------------
# SERMAYE VE RİSK PARAMETRELERİ
# ----------------------------------------------------------------------
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))

CAPITAL_ALLOCATION = {
    "scalp": 0.40,
    "day": 0.35,
    "swing": 0.25,
}

MIN_RISK_USD = float(os.getenv("MIN_RISK_USD", "10"))
RISK_PERCENT_PER_TRADE = {
    "scalp": 0.015,
    "day": 0.02,
    "swing": 0.025,
}

MAX_CONCURRENT_POSITIONS = {
    "scalp": 3,
    "day": 2,
    "swing": 2,
}

LEVERAGE_LOW_VOLATILITY = 20
LEVERAGE_HIGH_VOLATILITY = 10
VOLATILITY_ATR_THRESHOLD = 0.015

RISK_REWARD = {
    "scalp": {"tp1": 2.0, "tp2": 3.0},
    "day": {"tp1": 1.5, "tp2": 2.5},
    "swing": {"tp1": 2.0, "tp2": 3.5},
}

# ----------------------------------------------------------------------
# KILL-SWITCH (GÜVENLİK LİMİTLERİ) - SIKILAŞTIRILDI
# ----------------------------------------------------------------------
DAILY_MAX_LOSS_PERCENT = 0.15        # %30 -> %15 (daha erken dur)
ENGINE_CONSECUTIVE_LOSS_LIMIT = 2    # 3 -> 2 (daha erken dur)
ENGINE_PAUSE_DURATION_MINUTES = 120

# ----------------------------------------------------------------------
# CONFLUENCE (SİNYAL ONAY) EŞİKLERİ - DÜŞÜRÜLDÜ
# ----------------------------------------------------------------------
ENGINE_STRATEGIES = {
    "scalp": ["ema", "vwap", "support_resistance"],
    "day": ["ema", "vwap", "smc", "support_resistance"],
    "swing": ["smc", "liquidity_zones", "support_resistance", "fear_greed"],
}

CONFLUENCE_THRESHOLD = {
    "scalp": 2.0,   # 2.5 -> 2.0
    "day": 2.0,     # 2.5 -> 2.0
    "swing": 1.5,   # 2.0 -> 1.5
}

# ----------------------------------------------------------------------
# ZAMAN DİLİMLERİ
# ----------------------------------------------------------------------
ENGINE_TIMEFRAMES = {
    "scalp": {"entry": "1m", "confirm": "15m"},
    "day": {"entry": "15m", "confirm": "1h"},
    "swing": {"entry": "4h", "confirm": "1d"},
}

# ----------------------------------------------------------------------
# DİNAMİK PİYASA TARAMASI
# ----------------------------------------------------------------------
QUOTE_CURRENCY = "USDT"
TOP_SYMBOLS_COUNT = 15
MIN_24H_VOLUME_USDT = 100_000_000
SYMBOL_CACHE_TTL_SECONDS = 1800

EXCLUDE_BASE_ASSETS = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP"}

FALLBACK_SYMBOLS = ["BTC/USDT", "ETH/USDT"]

SYMBOL_COOLDOWN_MINUTES = 5

CORRELATED_GROUPS = [
    {"BTC/USDT", "ETH/USDT"},
]
MAX_SAME_DIRECTION_EXPOSURE_PERCENT = 0.25  # 0.15 -> 0.25 (daha esnek)

# ----------------------------------------------------------------------
# HABER/MAKRO FİLTRESİ
# ----------------------------------------------------------------------
NEWS_BLACKOUT_MINUTES_BEFORE = 30
NEWS_BLACKOUT_MINUTES_AFTER = 30

# ----------------------------------------------------------------------
# STOP LOSS MESAFESİ
# ----------------------------------------------------------------------
SL_ATR_MULTIPLIER = {
    "scalp": 1.8,
    "day": 1.8,
    "swing": 2.5,
}

# %0.4 -> %0.8 (daha geniş taban, gereksiz stop patlamasını önler)
MIN_STOP_DISTANCE_PERCENT = 0.008

# ----------------------------------------------------------------------
# SMC / LİKİDİTE / DESTEK-DİRENÇ
# ----------------------------------------------------------------------
SWING_LOOKBACK = 3
LIQUIDITY_EQUAL_TOLERANCE = 0.0015
SR_PROXIMITY_THRESHOLD = 0.002

# ----------------------------------------------------------------------
# KORKU-AÇGÖZLÜLÜK ENDEKSİ
# ----------------------------------------------------------------------
FEAR_GREED_API_URL = "https://api.alternative.me/fng/?limit=1"
FEAR_GREED_EXTREME_FEAR = 25
FEAR_GREED_EXTREME_GREED = 75

# ----------------------------------------------------------------------
# POZİSYON BÜYÜKLÜĞÜ GÜVENLİK TAVANI
# ----------------------------------------------------------------------
MAX_MARGIN_PERCENT_PER_TRADE = 0.20

# ----------------------------------------------------------------------
# HABER/MAKRO FİLTRESİ (JSON)
# ----------------------------------------------------------------------
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_HIGH_IMPACT_ONLY = True
NEWS_RELEVANT_CURRENCIES = {"USD"}

# ----------------------------------------------------------------------
# POZİSYON İZLEME
# ----------------------------------------------------------------------
POSITION_MONITOR_INTERVAL_SECONDS = 90
API_REQUEST_SPACING_SECONDS = 0.3

# Momentum geri çekilme eşiği (kârın ne kadarı geri verilirse kapat)
MOMENTUM_REVERSAL_RETRACE_PERCENT = 0.40

# ======================================================================
# KADEMELİ KÂR KORUMA SİSTEMİ (YENİ)
# ======================================================================
# Fiyat, stop mesafesinin şu oranı kadar kâra geçtiğinde izlemeye başla.
# Bu orana ulaşmadan hiçbir koruma devreye girmez (gürültüden korunma).
MOMENTUM_MIN_FAVORABLE_FRACTION = {
    "scalp": 0.5,   # Scalp için de aktif (önceki: None)
    "day": 0.5,
    "swing": 0.5,
}

# KADEMELİ KORUMA SEVİYELERİ
# Fiyat stop mesafesinin şu katı kadar kâra geçtiğinde stop'u nereye çekeceğimizi belirler.
# (ratio: favorable_move / stop_distance)
PROFIT_LOCK_LEVELS = [
    # (favorable_ratio_eşiği, stop_yeni_seviye_çarpanı)  -> yeni_stop = entry + (stop_dist * çarpan)
    (0.50, 0.00),   # %50 kâra geçince  -> stop = giriş (breakeven)
    (0.75, 0.25),   # %75 kâra geçince  -> stop = giriş + stop_dist * 0.25
    (1.00, 0.50),   # TP1 seviyesi      -> stop = giriş + stop_dist * 0.50 (kârın yarısı kilitli)
    (1.50, 0.75),   # %150 kâr          -> stop = giriş + stop_dist * 0.75
    (2.00, 1.00),   # TP2 seviyesi      -> stop = giriş + stop_dist * 1.00
]

# Kâr koruma kapatma: en iyi fiyattan bu oran kadar geri çekilirse kapat
PROFIT_PROTECT_CLOSE_RETRACE = 0.40   # %40 geri çekilme

# Bu kadar mumluk geçmiş, "en iyi favorable fiyatı" hesaplamak için taranır
MOMENTUM_LOOKBACK_CANDLES = 30

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

HEARTBEAT_INTERVAL_MINUTES = 30

WEEKLY_SUMMARY_DAY_OF_WEEK = "mon"
WEEKLY_SUMMARY_HOUR_UTC = 9
