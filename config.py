"""
Merkezi konfigürasyon dosyası (v2 - AI filtreli, "küçük ama net kazanç" modu).
Tüm hassas bilgiler (.env) dosyasından okunur, kod içine ASLA API key yazılmaz.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _env_float(name, default):
    return float(os.getenv(name, str(default)))


def _env_bool(name, default):
    return os.getenv(name, "true" if default else "false").lower() == "true"


# ----------------------------------------------------------------------
# BORSA
# ----------------------------------------------------------------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USE_TESTNET = _env_bool("USE_TESTNET", True)

# Testnet'in fiyat/hacim verisi gerçek piyasadan sapıyor. Sinyaller ve AI modeli
# GERÇEK piyasa verisiyle eğitildiği için, sinyal hesaplamada da gerçek (mainnet)
# mum verisini kullanıyoruz. Emirler yine testnet'e gider; SL/TP ise
# gerçekleşen dolum fiyatına göre yerleştirilir.
DATA_FROM_MAINNET = _env_bool("DATA_FROM_MAINNET", True)

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
HEARTBEAT_INTERVAL_MINUTES = 30
WEEKLY_SUMMARY_DAY_OF_WEEK = "mon"
WEEKLY_SUMMARY_HOUR_UTC = 9

# ----------------------------------------------------------------------
# VERİTABANI
# ----------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./crypto_bot.db"

# ----------------------------------------------------------------------
# İŞLEM MALİYETLERİ (backtest, AI etiketi ve PnL hesabında kullanılır)
# ----------------------------------------------------------------------
TAKER_FEE = _env_float("TAKER_FEE", 0.0005)       # Binance Futures VIP0 taker: %0.05
SLIPPAGE = _env_float("SLIPPAGE", 0.0002)         # tahmini kayma: taraf başına %0.02
COST_PER_SIDE = TAKER_FEE + SLIPPAGE

# ----------------------------------------------------------------------
# MOTORLAR
# ----------------------------------------------------------------------
# "Küçük kazanç" mantığı: hedefler yakın (1.2R - 1.5R), ama zarar asla
# kazançtan çok daha büyük değil. Eski sistemdeki "kârı 0.3R'de kes, zararı
# 1R'de al" yapısı kaldırıldı - o yapı matematiksel olarak kaybettiriyordu.
ENGINE_ENABLED = {
    "scalp": _env_bool("ENGINE_SCALP", True),
    "day": _env_bool("ENGINE_DAY", True),
    "swing": _env_bool("ENGINE_SWING", False),
}

ENGINE_TIMEFRAMES = {
    # 1 dakikalık scalp kaldırıldı: komisyon + kayma, 1m'deki her avantajı yiyor.
    "scalp": {"entry": "5m", "confirm": "1h"},
    "day": {"entry": "15m", "confirm": "4h"},
    "swing": {"entry": "1h", "confirm": "1d"},
}

# Her motorun kullanabileceği giriş kurulumları (backend/strategies/setups.py)
ENGINE_SETUPS = {
    "scalp": ["range_reversion", "liquidity_sweep", "trend_pullback"],
    "day": ["trend_pullback", "liquidity_sweep", "range_reversion"],
    "swing": ["trend_pullback", "liquidity_sweep"],
}

# Çıkış parametreleri. Backtest/AI eğitimi BİREBİR aynı parametreleri kullanır.
EXIT_PARAMS = {
    "scalp": {"sl_atr_mult": 1.5, "min_stop_pct": 0.006, "tp_r": 1.2, "max_hold_bars": 36, "breakeven_r": None},
    "day":   {"sl_atr_mult": 1.8, "min_stop_pct": 0.009, "tp_r": 1.5, "max_hold_bars": 32, "breakeven_r": None},
    "swing": {"sl_atr_mult": 2.0, "min_stop_pct": 0.015, "tp_r": 2.0, "max_hold_bars": 48, "breakeven_r": None},
}

# Stratejilerin ısınması için gereken mum sayısı (önceki günün pivotu dahil)
ENTRY_CANDLE_LIMIT = 1000
CONFIRM_CANDLE_LIMIT = 300

# ----------------------------------------------------------------------
# YAPAY ZEKA FİLTRESİ
# ----------------------------------------------------------------------
# "required": Onaylı (backtest'i geçmiş) model yoksa o motor İŞLEM AÇMAZ. (önerilen)
# "advisory": Model varsa filtre uygular, yoksa kurallarla işlem açar.
# "off":      AI kapalı, sadece kurallar.
AI_MODE = os.getenv("AI_MODE", "required")
AI_MODEL_DIR = os.getenv("AI_MODEL_DIR", "models")
# Modelin kendi eşiğine ek bir taban (0 = sadece modelin seçtiği eşik)
AI_MIN_PROBABILITY = _env_float("AI_MIN_PROBABILITY", 0.0)

# Modelin "onaylı" sayılması için test dönemindeki asgari performans
AI_APPROVAL = {"min_test_trades": 30, "min_avg_r": 0.05, "min_profit_factor": 1.10}

# ----------------------------------------------------------------------
# SERMAYE VE RİSK
# ----------------------------------------------------------------------
STARTING_BALANCE = _env_float("STARTING_BALANCE", 100)

RISK_PERCENT_PER_TRADE = {
    "scalp": _env_float("RISK_SCALP", 0.01),
    "day": _env_float("RISK_DAY", 0.01),
    "swing": _env_float("RISK_SWING", 0.01),
}

MAX_CONCURRENT_POSITIONS = {"scalp": 3, "day": 2, "swing": 1}

# Tüm açık işlemlerin toplam riski kasanın bu oranını geçemez
MAX_TOTAL_OPEN_RISK_PERCENT = 0.04
# Altcoinlerin hepsi BTC ile birlikte hareket eder: aynı yöndeki toplam risk tavanı
MAX_SAME_DIRECTION_RISK_PERCENT = 0.03

MAX_LEVERAGE = 10
MAX_MARGIN_PERCENT_PER_TRADE = 0.30
# Likidasyon mesafesi en az stop mesafesinin bu katı olmalı
LIQUIDATION_SAFETY_MULT = 3.0

# ----------------------------------------------------------------------
# KILL-SWITCH (TEKRAR AKTİF)
# ----------------------------------------------------------------------
DAILY_MAX_LOSS_PERCENT = _env_float("DAILY_MAX_LOSS_PERCENT", 0.05)
ENGINE_CONSECUTIVE_LOSS_LIMIT = 4
ENGINE_PAUSE_DURATION_MINUTES = 240

# ----------------------------------------------------------------------
# PİYASA TARAMASI
# ----------------------------------------------------------------------
QUOTE_CURRENCY = "USDT"
TOP_SYMBOLS_COUNT = 15
MIN_24H_VOLUME_USDT = 100_000_000
SYMBOL_CACHE_TTL_SECONDS = 1800
EXCLUDE_BASE_ASSETS = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP"}
FALLBACK_SYMBOLS = ["BTC/USDT", "ETH/USDT"]
SYMBOL_COOLDOWN_MINUTES = 15

# ----------------------------------------------------------------------
# HABER FİLTRESİ
# ----------------------------------------------------------------------
NEWS_BLACKOUT_MINUTES_BEFORE = 30
NEWS_BLACKOUT_MINUTES_AFTER = 30
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_HIGH_IMPACT_ONLY = True
NEWS_RELEVANT_CURRENCIES = {"USD"}

# ----------------------------------------------------------------------
# ZAMANLAMA
# ----------------------------------------------------------------------
POSITION_MONITOR_INTERVAL_SECONDS = 60
API_REQUEST_SPACING_SECONDS = 0.3
