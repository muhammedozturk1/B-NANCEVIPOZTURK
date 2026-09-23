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
MIN_RISK_USD = float(os.getenv("MIN_RISK_USD", "10"))
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
    # Gerçek veri: scalp motorunun kazanma oranı ~%36. Başabaş için gereken R/R = (1-0.36)/0.36 ≈ 1.78.
    # Güvenli marj için 2.0/3.0 seçildi - mevcut sinyal kalitesiyle bile matematiksel olarak kârlı olmalı.
    "scalp": {"tp1": 2.0, "tp2": 3.0},
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
    "scalp": 3,   # önceki 2/3 çok gevşekti (%33 kazanma oranı) - artık 3 teknikten HEPSİ aynı yönde onay vermeli
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
# DİNAMİK PİYASA TARAMASI (sabit coin listesi YOK)
# ----------------------------------------------------------------------
# Bot, hangi coin'lerde işlem yapacağını kendisi seçer: her tarama
# periyodunda Binance Futures'taki TÜM USDT paritelerinden, hacim ve
# likidite kriterlerine uyan en aktif N tanesini otomatik seçer.
QUOTE_CURRENCY = "USDT"
TOP_SYMBOLS_COUNT = 15               # önceki 25 çok genişti, kalite için azaltıldı
MIN_24H_VOLUME_USDT = 100_000_000    # önceki 20M çok düşüktü - meme/mikro-cap coinleri de kabul ediyordu,
                                       # bunlarda fiyat saniyeler içinde aşırı sıçrayıp anında TP/SL'e çarpıyordu
SYMBOL_CACHE_TTL_SECONDS = 1800     # tarama listesi 30 dakikada bir yenilenir (her cycle'da değil)

# Stablecoin'e karşı stablecoin işlemi anlamsız, bu bazlar hiç taranmaz
EXCLUDE_BASE_ASSETS = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP"}

# API/tarama başarısız olursa geriye düşülecek son çare liste (asla boş kalmasın diye)
FALLBACK_SYMBOLS = ["BTC/USDT", "ETH/USDT"]

# Bir sembolde işlem kapandıktan sonra, aynı sembolde (hangi motor olursa olsun)
# yeni işlem açılmadan önce beklenmesi gereken süre. Hızlı ardışık aç/kapa
# döngülerini (whipsaw) ve istenmeyen pozisyon birikmesini engeller.
SYMBOL_COOLDOWN_MINUTES = 5

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
# HEARTBEAT - detaylar Faz 4 bölümünde tanımlı
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# FAZ 2 - STOP LOSS MESAFESİ (ATR çarpanı, motor bazlı)
# ----------------------------------------------------------------------
SL_ATR_MULTIPLIER = {
    "scalp": 1.8,   # önceki 1.2 çok dardı, 1dk grafikte normal gürültüye bile takılıyordu
    "day": 1.8,
    "swing": 2.5,   # geniş stop, gürültüye takılmasın
}

# GÜVENLİK TABANI: ATR çok küçük çıksa bile (düşük volatilite algılanan an, veya
# hesaplama hatası), stop/TP mesafesi girişin bu yüzdesinden daha yakın olamaz.
# Bu, "işlem açılır açılmaz anında TP veya stop olma" sorununu önler.
MIN_STOP_DISTANCE_PERCENT = 0.004  # giriş fiyatının en az %0.4'ü kadar mesafe

# ----------------------------------------------------------------------
# SMC / LİKİDİTE / DESTEK-DİRENÇ PARAMETRELERİ
# ----------------------------------------------------------------------
SWING_LOOKBACK = 3            # fractal swing high/low tespiti için sağ-sol mum sayısı
LIQUIDITY_EQUAL_TOLERANCE = 0.0015   # eşit tepe/dip kabul toleransı (%0.15)
SR_PROXIMITY_THRESHOLD = 0.002       # fiyatın S/R seviyesine "yakın" sayılma mesafesi (%0.2)

# ----------------------------------------------------------------------
# KORKU-AÇGÖZLÜLÜK ENDEKSİ (Fear & Greed Index)
# ----------------------------------------------------------------------
FEAR_GREED_API_URL = "https://api.alternative.me/fng/?limit=1"
FEAR_GREED_EXTREME_FEAR = 25    # bu değerin altı -> aşırı korku (contrarian long)
FEAR_GREED_EXTREME_GREED = 75   # bu değerin üstü -> aşırı açgözlülük (contrarian short)

# ----------------------------------------------------------------------
# FAZ 3 - POZİSYON BÜYÜKLÜĞÜ GÜVENLİK TAVANI
# ----------------------------------------------------------------------
# Tek bir işlem, kasanın bu yüzdesinden fazla marj istemesin (küçük kasada
# aşırı büyük pozisyon açılmasını engeller - $100 kasada $87 marj istemek gibi
# durumları önler). Gerekirse pozisyon büyüklüğü bu sınıra göre küçültülür.
MAX_MARGIN_PERCENT_PER_TRADE = 0.20  # kasanın %20'si

# ----------------------------------------------------------------------
# FAZ 3 - HABER/MAKRO FİLTRESİ
# ----------------------------------------------------------------------
# ForexFactory'nin herkese açık haftalık takvim JSON'u (birçok açık kaynak
# bot bunu kullanır). Yüksek etkili USD haberlerinden 30dk önce/sonra yeni
# işlem açılmaz (mevcut açık işlemler etkilenmez).
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_HIGH_IMPACT_ONLY = True
NEWS_RELEVANT_CURRENCIES = {"USD"}

# ----------------------------------------------------------------------
# FAZ 3 - BREAKEVEN / MOMENTUM KAYBI / POZİSYON İZLEME
# ----------------------------------------------------------------------
POSITION_MONITOR_INTERVAL_SECONDS = 90  # önceki 30sn, Binance IP banına (418 hatası) yol açıyordu

# Motor döngüsünde sembol taraması sırasında art arda hızlı istek göndermemek için
# her sembol arasında küçük bir bekleme (saniye). Toplam istek sayısını değiştirmez,
# sadece saniye başına yoğunluğu düşürerek ban riskini azaltır.
API_REQUEST_SPACING_SECONDS = 0.3

# Fiyat, TP2'ye giden yoldaki en iyi noktadan bu oranın üzerinde geri
# çekilirse (kâr vermeden), pozisyon momentum kaybı nedeniyle kapatılır.
MOMENTUM_REVERSAL_RETRACE_PERCENT = 0.40

# KRİTİK: Momentum kaybı değerlendirmesi, fiyat stop mesafesinin en az bu kadarı
# (örn. %50'si) kadar lehimize gitmeden BAŞLAMAZ. Bu taban olmadan, çok küçük bir
# lehte hareket (örn. stop mesafesinin %2'si) bile "büyük ilerleme, şimdi geri
# çekiliyor" diye yanlış yorumlanıyor ve normal piyasa titreşiminde bile pozisyon
# anında (dakikalar içinde) kapatılıyordu - TÜM işlemlerin momentum-kaybıyla
# kapanmasının sebebi buydu.
MOMENTUM_MIN_FAVORABLE_FRACTION = 0.5

# Bu kadar mumluk geçmiş, "en iyi favorable fiyatı" hesaplamak için taranır
MOMENTUM_LOOKBACK_CANDLES = 30

# ----------------------------------------------------------------------
# FAZ 4 - TELEGRAM BİLDİRİMLERİ
# ----------------------------------------------------------------------
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Bot her X dakikada bir "hâlâ çalışıyorum" mesajı gönderir. Bu mesajlar
# kesilirse (Telegram'da uzun süre sessizlik olursa) bot çökmüş demektir -
# harici bir izleme servisi olmadan en basit ve güvenilir "hayattayım" sinyali budur.
HEARTBEAT_INTERVAL_MINUTES = 30

# Haftalık performans özeti ne zaman gönderilsin (UTC)
WEEKLY_SUMMARY_DAY_OF_WEEK = "mon"
WEEKLY_SUMMARY_HOUR_UTC = 9
