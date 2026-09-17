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

# Motor bazlı sermaye dağılımı (toplam 1.0 olmalı)
CAPITAL_ALLOCATION = {
    "scalp": 0.40,
    "day": 0.35,
    "swing": 0.25,
}

# İşlem başına risk: max(MIN_RISK_USD, kasa * RISK_PERCENT)
MIN_RISK_USD = float(os.getenv("MIN_RISK_USD", "5"))
RISK_PERCENT_PER_TRADE = {
    "scalp": 0.015,   # %1.5
    "day": 0.02,      # %2
    "swing": 0.025,   # %2.5
}

# Motor başına maksimum eşzamanlı açık pozisyon
MAX_CONCURRENT_POSITIONS = {
    "scalp": 3,
    "day": 2,
    "swing": 2,
}

# Kaldıraç seçimi (ATR/fiyat oranına göre otomatik)
LEVERAGE_LOW_VOLATILITY = 20   # dar range -> yüksek kaldıraç
LEVERAGE_HIGH_VOLATILITY = 10  # geniş range -> düşük kaldıraç
VOLATILITY_ATR_THRESHOLD = 0.015  # ATR/fiyat oranı bu değerin altındaysa "düşük volatilite"

# R/R oranları (motor bazlı, TP1/TP2 için)
RISK_REWARD = {
    "scalp": {"tp1": 1.2, "tp2": 2.0},
    "day": {"tp1": 1.5, "tp2": 2.5},
    "swing": {"tp1": 2.0, "tp2": 3.5},
}

# ----------------------------------------------------------------------
# KILL-SWITCH (GÜVENLİK LİMİTLERİ)
# ----------------------------------------------------------------------
DAILY_MAX_LOSS_PERCENT = 0.30       # Genel: kasanın %30'u kaybedilirse bot tamamen durur
ENGINE_CONSECUTIVE_LOSS_LIMIT = 3   # Bir motor art arda 3 kayıp yaparsa o motor geçici durur
ENGINE_PAUSE_DURATION_MINUTES = 120  # Durdurulan motor kaç dakika sonra tekrar denenir

# ----------------------------------------------------------------------
# CONFLUENCE (SİNYAL ONAY) EŞİKLERİ
# ----------------------------------------------------------------------
# Her motorun kullandığı teknikler ve sinyal açılması için gereken min. puan
ENGINE_STRATEGIES = {
    "scalp": ["ema", "vwap", "support_resistance"],
    "day": ["ema", "vwap", "smc", "support_resistance"],
    "swing": ["smc", "liquidity_zones", "support_resistance", "fear_greed"],
}
CONFLUENCE_THRESHOLD = {
    "scalp": 2,   # 3 teknikten en az 2'si aynı yönde onay vermeli
    "day": 3,     # 4 teknikten en az 3'ü
    "swing": 3,   # 4 teknikten en az 3'ü
}

# ----------------------------------------------------------------------
# ZAMAN DİLİMLERİ (her motor kendi ana + teyit TF'i kullanır)
# ----------------------------------------------------------------------
ENGINE_TIMEFRAMES = {
    "scalp": {"entry": "1m", "confirm": "15m"},
    "day": {"entry": "15m", "confirm": "1h"},
    "swing": {"entry": "4h", "confirm": "1d"},
}

# ----------------------------------------------------------------------
# İZLENECEK PARİTELER
# ----------------------------------------------------------------------
SYMBOLS = os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT").split(",")

# Korelasyonu yüksek kabul edilen parite grupları (aynı yönde toplam maruziyet kontrolü için)
CORRELATED_GROUPS = [
    {"BTC/USDT", "ETH/USDT"},
]
MAX_SAME_DIRECTION_EXPOSURE_PERCENT = 0.15  # Korelasyonlu paritelerde aynı yönde toplam risk tavanı

# ----------------------------------------------------------------------
# HABER/MAKRO FİLTRESİ
# ----------------------------------------------------------------------
NEWS_BLACKOUT_MINUTES_BEFORE = 30
NEWS_BLACKOUT_MINUTES_AFTER = 30

# ----------------------------------------------------------------------
# HEARTBEAT
# ----------------------------------------------------------------------
HEARTBEAT_INTERVAL_MINUTES = 30
