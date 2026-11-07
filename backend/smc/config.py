"""SMC Scalper konfigürasyonu.

SMC'ye özel tüm parametreler `SMC_` önekli environment variable'lardan okunur
(örn. SMC_RISK_PER_TRADE). Mevcut botla ORTAK kullanılan değerler öneksiz okunur:
BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL,
DASHBOARD_USERNAME, DASHBOARD_PASSWORD_HASH, SESSION_SECRET, SESSION_HTTPS_ONLY.
Gizli değerler asla repoya yazılmaz; Render Environment Variables üzerinden verilir.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Emir gönderilebilecek tek hostlar: Binance Futures Testnet / Demo.
# Mainnet'e emir gönderimi kod seviyesinde engellenir.
ALLOWED_TRADING_HOSTS = frozenset({"testnet.binancefuture.com", "demo-fapi.binance.com"})


# "active" profilde değişen eşikler. Zorunlu kontroller (15m yapı, 5m BOS/CHoCH, sweep, OTE, 3m onay)
# ve risk limitleri (%5 / %15 / %10, 10x, liq buffer) değişmez.
ACTIVE_PROFILE = {
    "ote_low": 0.50,             # giriş bölgesi %62–79 yerine %50–79
    "confirm_lookback_3m": 12,   # OTE dokunuşu son 6 yerine son 12 adet 3m mumda aranır (36 dk)
    "trigger_break_bars": 1,     # onay mumu son 2 yerine son 1 mumun high/low'unu kırmalı
    "max_stop_pct": 0.04,        # stop en fazla %3 yerine %4 uzaklıkta olabilir
    "sweep_lookback": 60,        # likidite süpürmesi son 3 saat yerine son 5 saatte aranır
    "slippage_pct": 0.0002,      # taraf başı tahmini kayma %0.05 yerine %0.02 (likit top-25 coin; ana botla aynı)
    "min_rr": 1.0,               # maliyet SONRASI ağırlıklı RR alt sınırı 1.2 yerine 1.0
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_prefix="SMC_", populate_by_name=True)

    # ------------------------------------------------------------- entegrasyon
    enabled: bool = True  # SMC_ENABLED=false -> mevcut bot SMC thread'ini başlatmaz
    # shared: aynı testnet hesabında başka motorlar (mevcut scalp/day/swing) da işlem açabilir.
    #   Kill switch / reconciliation yalnızca SMC'nin kendi pozisyon ve emirlerine dokunur,
    #   başka bir botun pozisyonu olan sembollerde SMC işlem açmaz.
    # exclusive: hesap yalnızca SMC'ye ait; şartnamedeki hesap genelinde "0 pozisyon / 0 emir" doğrulaması.
    account_scope: Literal["shared", "exclusive"] = "shared"
    # Strateji profili (SMC_PROFILE):
    #   spec   : şartname v2.1 eşikleri birebir (çok seçici, işlem nadir).
    #   active : zorunlu kontroller ve risk kuralları AYNI; yalnızca eşikler gevşek (bkz. ACTIVE_PROFILE).
    # Bir parametre env ile açıkça verilirse (örn. SMC_MAX_STOP_PCT) profil onu ezmez.
    profile: Literal["spec", "active"] = "active"
    # Strateji (SMC_STRATEGY):
    #   breakout : 4 saatlik kanal kırılımı, stop = kasanın %1'i, tek hedef = kasanın %2'si (net). Varsayılan.
    #   smc      : eski SMC/ICT pipeline (3m/5m/15m, TP1/TP2/TP3).
    # Not: 1dk scalp kuralları ve 1s/15dk trend takibi geçmiş testte zarar etti (README, v4.3).
    strategy: Literal["breakout", "smc"] = "breakout"
    bo_tf: Literal["4h"] = "4h"
    bo_channel: int = 20              # önceki N adet 4s mumun tepesi/dibi
    bo_stop_atr: float = 3.0          # stop = kapanış -/+ K x ATR(14)
    bo_risk_per_trade: float = 0.01   # stop olursa kasanın bu kadarı (komisyon dahil)
    bo_target_r: float = 2.0          # hedef = net kâr, risk tutarının bu katı (kasanın %2'si)
    bo_max_new_per_bar: int = 1       # aynı 4s kapanışında en fazla bu kadar yeni işlem
    bo_fng_filter: bool = True
    bo_min_stop_pct: float = 0.004
    bo_max_stop_pct: float = 0.25     # geniş stoplarda kaldıraç likidasyon tamponu için otomatik düşer

    # ------------------------------------------------------------------ ortam
    trading_env: Literal["testnet", "demo"] = "testnet"
    binance_rest_url: str = "https://testnet.binancefuture.com"
    binance_ws_market_url: str = "wss://fstream.binancefuture.com"
    binance_ws_user_url: str = "wss://fstream.binancefuture.com"
    # Sadece okuma amaçlı public market data (universe hacim sıralaması, backtest verisi)
    public_data_rest_url: str = "https://fapi.binance.com"
    binance_api_key: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("SMC_BINANCE_API_KEY", "BINANCE_API_KEY"))
    binance_api_secret: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("SMC_BINANCE_API_SECRET", "BINANCE_API_SECRET"))
    recv_window_ms: int = 5000
    # Aralık 2025'ten itibaren Binance koşullu emirleri (STOP_MARKET, TAKE_PROFIT_MARKET)
    # /fapi/v1/algoOrder üzerinden kabul eder. False yapılırsa eski /fapi/v1/order kullanılır.
    use_algo_orders: bool = True
    sl_working_type: Literal["MARK_PRICE", "CONTRACT_PRICE"] = "MARK_PRICE"

    database_url: str = Field("sqlite:///./crypto_bot.db", validation_alias=AliasChoices("SMC_DATABASE_URL", "DATABASE_URL"))

    telegram_bot_token: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("SMC_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = Field("", validation_alias=AliasChoices("SMC_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"))

    dashboard_username: str = Field("admin", validation_alias=AliasChoices("SMC_DASHBOARD_USERNAME", "DASHBOARD_USERNAME"))
    # python -m scripts.smc.hash_password ile üretilir: pbkdf2_sha256$iter$salt$hash
    dashboard_password_hash: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("SMC_DASHBOARD_PASSWORD_HASH", "DASHBOARD_PASSWORD_HASH"))
    session_secret: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("SMC_SESSION_SECRET", "SESSION_SECRET"))
    session_https_only: bool = Field(True, validation_alias=AliasChoices("SMC_SESSION_HTTPS_ONLY", "SESSION_HTTPS_ONLY"))

    # ------------------------------------------------------------- hesap/risk
    starting_equity: float = 100.0
    # virtual: Testnet hesabında bakiye ne olursa olsun bot 100 USDT başlangıç sermayesiyle
    # (starting_equity + botun gerçekleşmiş net PnL'i + açık pozisyon unrealized PnL) çalışır.
    # account: doğrudan Binance hesap equity'si kullanılır.
    equity_mode: Literal["virtual", "account"] = "virtual"
    margin_mode: Literal["ISOLATED"] = "ISOLATED"  # CROSS kesinlikle desteklenmez
    position_mode: Literal["ONE_WAY"] = "ONE_WAY"
    risk_per_trade: float = 0.05
    max_open_positions: int = 3
    max_total_open_risk: float = 0.15
    daily_loss_limit: float = 0.10
    max_leverage: int = 10
    max_consecutive_sl: int = 3
    cooldown_minutes: int = 30
    max_position_notional: float = 1500.0
    max_margin_usage: float = 0.95  # available balance'ın kullanılabilecek maksimum oranı
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    slippage_pct: float = 0.0005
    # Ağırlıklı (50/25/25 @ 1R/2R/3R → brüt 1.75), komisyon + slippage SONRASI planlanan RR.
    # Çok dar stoplarda maliyetler RR'yi düşürür ve işlem otomatik reddedilir.
    min_rr: float = 1.2
    min_stop_pct: float = 0.0015
    max_stop_pct: float = 0.03
    liq_buffer_pct: float = 0.005  # SL ile liq arası minimum mesafe (entry'nin yüzdesi)
    liq_buffer_stop_ratio: float = 0.5  # ...ve stop mesafesinin en az bu katı
    max_spread_pct: float = 0.0006
    min_book_depth_usdt: float = 20_000.0  # ±%0.5 içindeki toplam derinlik
    correlation_threshold: float = 0.80
    correlated_risk_factor: float = 0.5
    max_correlated_same_direction: int = 2  # 3. korelasyonlu aynı yön pozisyon engellenir
    correlation_lookback: int = 100

    # ------------------------------------------------------------------ TP/BE
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    tp3_r: float = 3.0
    tp1_pct: float = 0.50
    tp2_pct: float = 0.25
    tp3_pct: float = 0.25
    move_sl_to_tp1_after_tp2: bool = True

    # ------------------------------------------------------------------ trial
    # Eski 72 saatlik deneme kilidi. Varsayılan KAPALI: bot süresiz çalışır (SMC_TRIAL_ENABLED=true ile açılır).
    trial_enabled: bool = False
    trial_hours: int = 72

    # --------------------------------------------------------------- universe
    universe_size: int = 25
    min_24h_quote_volume: float = 100_000_000.0
    universe_volume_source: Literal["public", "trading"] = "public"
    universe_refresh_minutes: int = 60
    symbol_whitelist: str = ""  # virgülle ayrılmış; boşsa otomatik
    symbol_blacklist: str = "USDCUSDT,BTCDOMUSDT"

    # --------------------------------------------------------------- strateji
    pivot_left: int = 2
    pivot_right: int = 2
    ote_low: float = 0.62
    ote_high: float = 0.79
    ote_preferred: float = 0.705
    displacement_body_mult: float = 1.5
    displacement_close_location: float = 0.70
    body_avg_period: int = 20
    volume_avg_period: int = 20
    volume_mult: float = 1.2
    cvd_window: int = 50
    cvd_slope_lookback: int = 20
    min_score: int = 9
    sweep_lookback: int = 36  # 5m bar
    sweep_reclaim_bars: int = 2
    equal_level_tolerance: float = 0.0005
    min_leg_atr: float = 1.5
    sl_buffer_atr: float = 0.10
    atr_period: int = 14
    confirm_lookback_3m: int = 6  # OTE'ye dokunuşun aranacağı son 3m mum sayısı
    trigger_break_bars: int = 2  # 3m onay mumu son kaç mumun high/low'unu kırmalı
    sessions_utc: str = "0-7,7-12,12-20,20-24"
    oi_change_threshold: float = 0.002
    funding_extreme: float = 0.0005
    fng_enabled: bool = True
    fng_url: str = "https://api.alternative.me/fng/?limit=1"
    fng_fallback_points: int = 0  # harici API çalışmazsa makro puanı
    fng_extreme_greed: int = 80
    fng_extreme_fear: int = 20

    # -------------------------------------------------------------- execution
    entry_limit_offset_pct: float = 0.0005
    entry_order_timeout_s: float = 10.0
    order_retry_attempts: int = 3
    require_ws_connected: bool = True
    position_sync_interval_s: float = 3.0
    heartbeat_interval_s: float = 10.0
    heartbeat_stale_s: float = 60.0
    time_sync_interval_s: float = 300.0
    max_clock_offset_ms: int = 1000
    market_data_stale_factor: float = 2.5
    rate_limit_weight_per_min: int = 2000  # Binance limiti 2400; güvenlik payı
    rate_limit_orders_per_10s: int = 250
    rate_limit_orders_per_min: int = 1000

    # --------------------------------------------------------------- backtest
    bt_partial_fill_prob: float = 0.10
    bt_seed: int = 42
    bt_funding_rate_8h: float = 0.0001

    log_level: str = "INFO"

    # ---------------------------------------------------------------- doğrulama
    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.profile == "active":
            for key, value in ACTIVE_PROFILE.items():
                if key not in self.model_fields_set:
                    setattr(self, key, value)
        if self.strategy == "breakout":
            # Bu stratejide risk ve hedef yapısı sabittir: %1 risk, tek hedef (TP1 = pozisyonun tamamı).
            # SMC_RISK_PER_TRADE (eski SMC ayarı) burada kullanılmaz; değiştirmek için SMC_BO_RISK_PER_TRADE.
            self.risk_per_trade = self.bo_risk_per_trade
            self.tp1_pct, self.tp2_pct, self.tp3_pct = 1.0, 0.0, 0.0
            self.tp1_r = self.tp2_r = self.tp3_r = self.bo_target_r
            self.min_rr = min(self.min_rr, self.bo_target_r * 0.95)
        host = urlparse(self.binance_rest_url).hostname or ""
        if host not in ALLOWED_TRADING_HOSTS:
            raise ValueError(
                f"BINANCE_REST_URL '{host}' izinli değil. Bu bot yalnızca Testnet/Demo üzerinde emir gönderir: "
                f"{sorted(ALLOWED_TRADING_HOSTS)}"
            )
        if not 1 <= self.max_leverage <= 10:
            raise ValueError("max_leverage 1..10 aralığında olmalı")
        if abs(self.tp1_pct + self.tp2_pct + self.tp3_pct - 1.0) > 1e-9:
            raise ValueError("TP yüzdeleri toplamı 1.0 olmalı")
        if not (0 < self.ote_low < self.ote_preferred < self.ote_high < 1):
            raise ValueError("OTE aralığı geçersiz")
        if self.max_total_open_risk < self.risk_per_trade:
            raise ValueError("max_total_open_risk >= risk_per_trade olmalı")
        if self.database_url.startswith("postgres://"):  # SQLAlchemy 'postgres://' kabul etmez (psycopg2)
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]
        return self

    @property
    def whitelist(self) -> list[str]:
        return [s.strip().upper() for s in self.symbol_whitelist.split(",") if s.strip()]

    @property
    def blacklist(self) -> set[str]:
        return {s.strip().upper() for s in self.symbol_blacklist.split(",") if s.strip()}

    @property
    def sessions(self) -> list[tuple[int, int]]:
        out = []
        for part in self.sessions_utc.split(","):
            a, b = part.split("-")
            out.append((int(a), int(b)))
        return out

    def public_dict(self) -> dict:
        """Gizli alanlar hariç config (DB snapshot ve dashboard için)."""
        data = self.model_dump()
        for k in list(data):
            if any(s in k for s in ("secret", "key", "token", "password", "database_url")):
                data[k] = "***"
        return data


@lru_cache
def get_settings() -> Settings:
    return Settings()
