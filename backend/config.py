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
    # Scalp kapalı: 3 ayrı eğitimde de test döneminde zarar etti (komisyon kazancı yiyor).
    "scalp": _env_bool("ENGINE_SCALP", False),
    # Day/Swing kapalı: 100 coin / 2-3 yıllık testte tutarlı avantaj göstermediler.
    "day": _env_bool("ENGINE_DAY", False),
    "swing": _env_bool("ENGINE_SWING", False),
}

ENGINE_TIMEFRAMES = {
    # 1 dakikalık scalp kaldırıldı: komisyon + kayma, 1m'deki her avantajı yiyor.
    "scalp": {"entry": "5m", "confirm": "1h"},
    "day": {"entry": "15m", "confirm": "4h"},
    "swing": {"entry": "1h", "confirm": "1d"},
}

# Her motorun kullanabileceği giriş kurulumları (backend/strategies/setups.py).
# NOT: Onaylı bir AI modeli varsa, bot bu listeyi DEĞİL modelin eğitimde
# doğrulama döneminde kârlı bulduğu kurulumları kullanır (model dosyasında saklı).
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
# (30 işlem çok azdı - şansla geçilebiliyordu. Daha fazla coin = daha fazla test işlemi.)
AI_APPROVAL = {"min_test_trades": 60, "min_avg_r": 0.05, "min_profit_factor": 1.15}

# ----------------------------------------------------------------------
# FONLAMA ORANI (FUNDING) STRATEJİSİ - v3 ana strateji
# ----------------------------------------------------------------------
# Aynı coin spot'ta ALINIR, vadelide aynı miktar SHORT açılır. Fiyat ne yöne
# giderse gitsin iki bacak birbirini dengeler; bot sadece vadeli short'un her
# 4/8 saatte aldığı fonlama ödemesini toplar. Fiyat tahmini YOK.
#
# "paper" modu: gerçek Binance (mainnet) fiyat ve fonlama verisiyle SANAL işlem.
# Testnet'in fonlama oranları gerçek değil; bu strateji için paper modu
# testnetten DAHA gerçekçidir. Emir gönderilmez, API key gerekmez.
FUNDING = {
    "enabled": _env_bool("FUNDING_ENABLED", True),
    "mode": os.getenv("FUNDING_MODE", "paper"),
    "max_positions": int(os.getenv("FUNDING_MAX_POSITIONS", "5")),
    "position_pct": _env_float("FUNDING_POSITION_PCT", 0.12),  # her pozisyonun nominali (kasa oranı)
    "perp_leverage": 2,              # short bacak teminatı = nominal / 2 -> pozisyon başı sermaye 1.5x nominal
    "lookback_hours": 72,            # karar için son 3 günün ortalama fonlama oranı
    "entry_min_apr": _env_float("FUNDING_ENTRY_APR", 0.15),   # yıllık %15 altı -> komisyonu çıkarmaz
    "entry_last_min_apr": 0.10,      # son ödeme de en az yıllık %10 olmalı
    "exit_apr": 0.03,                # 3 günlük ortalama yıllık %3'ün altına düşerse çık
    "min_hold_hours": 72,            # komisyonu çıkarmak için en az 3 gün tut (güçlü negatif hariç)
    "max_price_move": 0.30,          # fiyat %30 oynarsa çık (short bacak likidasyon koruması)
    "spot_fee": 0.001,               # Binance spot taker %0.10
    "perp_fee": 0.0005,              # Binance futures taker %0.05
    "slippage": 0.0003,              # bacak başı tahmini kayma
    "max_history_checks": 30,        # döngü başına en fazla bu kadar coinin geçmişine bak
}

# ----------------------------------------------------------------------
# SERMAYE VE RİSK
# ----------------------------------------------------------------------
STARTING_BALANCE = _env_float("STARTING_BALANCE", 100)

RISK_PERCENT_PER_TRADE = {
    "scalp": _env_float("RISK_SCALP", 0.01),
    "day": _env_float("RISK_DAY", 0.01),
    "swing": _env_float("RISK_SWING", 0.01),
}

MAX_CONCURRENT_POSITIONS = {"scalp": 3, "day": 3, "swing": 3}

# Tüm açık işlemlerin toplam riski kasanın bu oranını geçemez
MAX_TOTAL_OPEN_RISK_PERCENT = 0.05
# Altcoinlerin hepsi BTC ile birlikte hareket eder: aynı yöndeki toplam risk tavanı
MAX_SAME_DIRECTION_RISK_PERCENT = 0.04

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
TOP_SYMBOLS_COUNT = 100           # en fazla bu kadar parite (pratikte likit marketin tamamı)
MIN_24H_VOLUME_USDT = 20_000_000  # bunun altı sığ: stop'lar kayarak vuruluyor
SYMBOL_CACHE_TTL_SECONDS = 1800
EXCLUDE_BASE_ASSETS = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP"}
# TARAMA: Sabit liste YOK - tüm Binance Futures USDT perpetual marketi taranır
# (backend/exchange/universe.py). Elenenler: kripto olmayan kontratlar (altın, petrol,
# hisse), sığ coinler ve yeni listelenenler.
MIN_LISTING_DAYS = 120
# Ek güvenlik: borsa verisinde işaretlenmemiş olabilecek kripto-dışı kontratlar
NON_CRYPTO_BLACKLIST = {
    "XAU", "XAG", "XPT", "XPD", "CL", "BZ", "NG", "COPPER",
    "SOXL", "SNDK", "SPCX", "TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META",
    "COIN", "MSTR", "HOOD", "QQQ", "SPY",
}
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
