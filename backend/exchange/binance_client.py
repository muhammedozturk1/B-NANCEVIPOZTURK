"""
Binance Futures bağlantı katmanı.
Testnet / canlı geçişi tek bir config değişkeni (USE_TESTNET) ile yapılır.
Tüm borsa işlemleri bu sınıf üzerinden geçer -> ileride başka borsa eklemek
istersek sadece bu dosyayı değiştiririz, strateji kodlarına dokunmayız.
"""
import time
import hmac
import hashlib
import requests
import ccxt
import logging
from backend import config

logger = logging.getLogger("binance_client")


class UnprotectedPositionError(Exception):
    """Pozisyon açıldı, stop-loss konulamadı ve pozisyon da geri kapatılamadı."""


class BinanceClient:
    def __init__(self):
        self.exchange = ccxt.binance({
            "apiKey": config.BINANCE_API_KEY,
            "secret": config.BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })

        if config.USE_TESTNET:
            self.exchange.set_sandbox_mode(True)
            self.base_url = "https://testnet.binancefuture.com"
            logger.info("Binance TESTNET modunda başlatıldı.")
        else:
            self.base_url = "https://fapi.binance.com"
            logger.warning("Binance CANLI modda başlatıldı - gerçek para kullanılıyor!")

        self.exchange.load_markets()

        # Sinyal verisi: testnet'teyken bile gerçek piyasa mumları (config.DATA_FROM_MAINNET)
        if config.USE_TESTNET and config.DATA_FROM_MAINNET:
            self.data_exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
            self.data_exchange.load_markets()
            logger.info("Sinyal verisi: Binance GERÇEK piyasa (emirler testnet'e gider).")
        else:
            self.data_exchange = self.exchange

    # ------------------------------------------------------------------
    # PİYASA VERİSİ
    # ------------------------------------------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200):
        """Mum verisi çeker: [timestamp, open, high, low, close, volume]
        DİKKAT: son eleman henüz kapanmamış mumdur; stratejiler drop_unclosed() ile atar."""
        return self.data_exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def min_notional(self, symbol: str) -> float:
        try:
            m = self.exchange.market(symbol)
            v = (m.get("limits", {}).get("cost", {}) or {}).get("min")
            if v:
                return float(v)
            for f in m.get("info", {}).get("filters", []):
                if f.get("filterType") == "MIN_NOTIONAL":
                    return float(f.get("notional") or f.get("minNotional") or 5)
        except Exception:
            pass
        return 5.0

    def fetch_order_book(self, symbol: str, limit: int = 100):
        return self.exchange.fetch_order_book(symbol, limit=limit)

    def fetch_ticker(self, symbol: str):
        return self.exchange.fetch_ticker(symbol)

    # ------------------------------------------------------------------
    # HESAP / BAKİYE
    # ------------------------------------------------------------------
    def fetch_balance(self):
        return self.exchange.fetch_balance()

    def get_usdt_balance(self) -> float:
        balance = self.fetch_balance()
        return float(balance.get("USDT", {}).get("free", 0))

    # ------------------------------------------------------------------
    # KALDIRAÇ AYARI
    # ------------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int):
        try:
            self.exchange.set_leverage(leverage, symbol)
        except Exception as e:
            logger.error(f"Kaldıraç ayarlanamadı ({symbol}, {leverage}x): {e}")

    # ------------------------------------------------------------------
    # ALGO ORDER API (SL/TP) - Binance'in Aralık 2025'te zorunlu kıldığı sistem
    # ------------------------------------------------------------------
    def _signed_algo_request(self, method: str, path: str, params: dict):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        signature = hmac.new(
            config.BINANCE_API_SECRET.encode(), query_string.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
        url = f"{self.base_url}{path}"

        response = requests.request(method, url, params=params, headers=headers, timeout=10)
        if not response.ok:
            logger.error(f"Algo order isteği başarısız ({response.status_code}): {response.text}")
        response.raise_for_status()
        return response.json()

    def _place_algo_stop_order(self, symbol: str, entry_side: str, amount: float,
                                trigger_price: float, order_type: str):
        """
        order_type: 'STOP_MARKET' veya 'TAKE_PROFIT_MARKET'

        ÖNEMLİ DEĞİŞİKLİK (Kâr Koruma):
        Take-profit artık TAKE_PROFIT_MARKET olarak gönderiliyor (eskiden LIMIT'ti).
        Sebep: LIMIT TP emri, fiyat TP seviyesine DEĞDİĞİ anda tetiklenmiyordu;
        fiyatın o seviyede kalması gerekiyordu. Fiyat TP1'e değip anında geri
        dönerse TP hiç gerçekleşmiyor, sonra fiyat ters yöne gidip stop patlıyordu.
        MARKET TP, fiyat TP1'e değdiği anda kârı realize eder ve BE mekanizması
        devreye girer.
        """
        close_side = "SELL" if entry_side == "buy" else "BUY"
        market_symbol = self.exchange.market(symbol)["id"]

        rounded_trigger = self.exchange.price_to_precision(symbol, trigger_price)
        rounded_amount = self.exchange.amount_to_precision(symbol, amount)

        params = {
            "algoType": "CONDITIONAL",
            "symbol": market_symbol,
            "side": close_side,
            "type": order_type,
            "triggerPrice": rounded_trigger,
            "quantity": rounded_amount,
            "reduceOnly": "true",
            # MARK_PRICE yerine CONTRACT_PRICE (gerçek son işlem fiyatı) kullanıyoruz.
            # Testnet'te Mark Price, ince/sığ likidite yüzünden gerçekçi olmayan ani
            # sıçramalar yapabiliyor ve bu da fiyat aslında hedefe hiç gelmeden emri
            # yanlışlıkla tetikliyordu.
            "workingType": "CONTRACT_PRICE",
        }

        return self._signed_algo_request("POST", "/fapi/v1/algoOrder", params)

    def get_open_algo_orders(self, symbol: str) -> list:
        market_symbol = self.exchange.market(symbol)["id"]
        result = self._signed_algo_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": market_symbol})
        return result if isinstance(result, list) else result.get("data", [])

    def cancel_algo_order(self, algo_id):
        return self._signed_algo_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})

    def get_algo_order(self, algo_id: int):
        """
        Bir algo emrin GERÇEK durumunu ve gerçekleşme fiyatını sorgular.
        algoStatus: NEW / TRIGGERED / FINISHED / CANCELED
        actualPrice: emir gerçekten tetiklenip yerine getirildiyse gerçek fiyat
        """
        return self._signed_algo_request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id})

    def cancel_open_stop_orders(self, symbol: str):
        """
        Bu sembol için açık STOP_MARKET emirlerini iptal eder.
        TAKE_PROFIT emrine DOKUNMAZ - breakeven güncellemesinde TP hedefi kaybolmasın diye.
        """
        try:
            orders = self.get_open_algo_orders(symbol)
            for order in orders:
                if order.get("orderType") == "STOP_MARKET":
                    self.cancel_algo_order(order["algoId"])
        except Exception as e:
            logger.error(f"{symbol}: açık stop emirleri iptal edilemedi: {e}")

    def cancel_all_algo_orders(self, symbol: str):
        """
        SL VE TP dahil, bu sembol için açık tüm algo emirlerini iptal eder.
        Pozisyonu tamamen manuel kapatırken (momentum kaybı vb.) kullanılır.
        """
        try:
            orders = self.get_open_algo_orders(symbol)
            for order in orders:
                self.cancel_algo_order(order["algoId"])
        except Exception as e:
            logger.error(f"{symbol}: açık algo emirleri iptal edilemedi: {e}")

    # ------------------------------------------------------------------
    # POZİSYON AÇMA / KAPAMA
    # ------------------------------------------------------------------
    def open_position(self, symbol: str, side: str, amount: float,
                       stop_distance: float, tp_distance: float):
        """
        Market emirle pozisyon açar, SL/TP'yi GERÇEK DOLUM FİYATINA göre yerleştirir.
        (Eski kod SL/TP'yi son mum kapanışına göre koyuyordu; dolum fiyatı farklıysa
        risk/ödül oranı bozuluyordu.)

        Dönüş: {"order", "fill_price", "amount", "stop_loss", "take_profit", "sl_algo_id", "tp_algo_id"}
        """
        rounded_amount = float(self.exchange.amount_to_precision(symbol, amount))
        if rounded_amount <= 0:
            raise ValueError(f"{symbol}: miktar hassasiyete yuvarlanınca 0 oldu")

        order = self.exchange.create_order(
            symbol=symbol, type="market", side=side, amount=rounded_amount,
            params={"newOrderRespType": "RESULT"},
        )
        fill_price = order.get("average") or order.get("price")
        if not fill_price:
            try:
                fill_price = self.exchange.fetch_ticker(symbol)["last"]
            except Exception:
                fill_price = None
        fill_price = float(fill_price)
        logger.info(f"Pozisyon açıldı: {symbol} {side} {rounded_amount} @ {fill_price}")

        d = 1 if side == "buy" else -1
        stop_loss = fill_price - d * stop_distance
        take_profit = fill_price + d * tp_distance
        amount = rounded_amount
        sl_algo_id, tp_algo_id = None, None

        # Borsa ara sıra 429/geçici hata verebiliyor. Stop yerleşmezse pozisyon korumasız kalır.
        sl_placed, last_error = False, None
        for attempt in range(3):
            try:
                resp = self._place_algo_stop_order(symbol, side, amount, stop_loss, order_type="STOP_MARKET")
                sl_algo_id = resp.get("algoId")
                sl_placed = True
                break
            except Exception as e:
                last_error = e
                logger.warning(f"{symbol}: stop-loss denemesi {attempt + 1}/3 başarısız: {e}")
                time.sleep(1.5 * (attempt + 1))

        if not sl_placed:
            closed = False
            for attempt in range(3):
                try:
                    self.close_position(symbol, side, amount)
                    closed = True
                    break
                except Exception as e:
                    logger.error(f"{symbol}: korumasız pozisyon kapatma denemesi {attempt + 1}/3 başarısız: {e}")
                    time.sleep(1.5 * (attempt + 1))
            if closed:
                raise RuntimeError(f"{symbol}: stop-loss konulamadı, pozisyon güvenlik için geri kapatıldı. Hata: {last_error}")
            raise UnprotectedPositionError(
                f"{symbol} {side} {amount}: stop-loss konulamadı VE pozisyon kapatılamadı, "
                f"pozisyon KORUMASIZ. Elle kapatın. Hata: {last_error}")

        for attempt in range(3):
            try:
                resp = self._place_algo_stop_order(symbol, side, amount, take_profit, order_type="TAKE_PROFIT_MARKET")
                tp_algo_id = resp.get("algoId")
                break
            except Exception as e:
                logger.error(f"❌ {symbol}: take-profit denemesi {attempt + 1}/3 başarısız: {e}")
                time.sleep(1.5 * (attempt + 1))

        return {"order": order, "fill_price": fill_price, "amount": amount,
                "stop_loss": stop_loss, "take_profit": take_profit,
                "sl_algo_id": sl_algo_id, "tp_algo_id": tp_algo_id}

    def close_position(self, symbol: str, side: str, amount: float):
        close_side = "sell" if side == "buy" else "buy"
        return self.exchange.create_order(
            symbol=symbol,
            type="market",
            side=close_side,
            amount=amount,
            params={"reduceOnly": True},
        )

    def update_stop_loss(self, symbol: str, side: str, amount: float, new_stop_price: float,
                         old_algo_id=None) -> int:
        """
        Stop'u taşır. ÖNCE yeni stop konur, SONRA eskisi iptal edilir.
        (Eski kod önce iptal ediyordu; yeni emir başarısız olursa pozisyon korumasız kalıyordu.)
        """
        resp = self._place_algo_stop_order(symbol, side, amount, new_stop_price, order_type="STOP_MARKET")
        new_id = resp.get("algoId")
        try:
            if old_algo_id:
                self.cancel_algo_order(old_algo_id)
            else:
                for order in self.get_open_algo_orders(symbol):
                    if order.get("orderType") == "STOP_MARKET" and order.get("algoId") != new_id:
                        self.cancel_algo_order(order["algoId"])
        except Exception as e:
            logger.warning(f"{symbol}: eski stop iptal edilemedi (yeni stop aktif): {e}")
        return new_id

    def fetch_realized_pnl(self, symbol: str, since_ms: int):
        """Pozisyon açılışından beri borsanın bildirdiği GERÇEK net PnL ve komisyon.
        Dönüş: (net_pnl, commission, son_fiyat) veya bulunamazsa None."""
        trades = self.exchange.fetch_my_trades(symbol, since=since_ms, limit=100)
        if not trades:
            return None
        realized = sum(float(t.get("info", {}).get("realizedPnl") or 0) for t in trades)
        commission = 0.0
        for t in trades:
            fee = t.get("fee") or {}
            if (fee.get("currency") or "USDT") == "USDT":
                commission += float(fee.get("cost") or 0)
        closing = [t for t in trades if float(t.get("info", {}).get("realizedPnl") or 0) != 0]
        last_price = float(closing[-1]["price"]) if closing else float(trades[-1]["price"])
        return realized - commission, commission, last_price

    # ------------------------------------------------------------------
    # DİNAMİK PİYASA TARAMASI
    # ------------------------------------------------------------------
    def fetch_top_symbols(self, limit: int, min_volume: float, quote: str = "USDT",
                           exclude_base_assets: set = None) -> list:
        """
        Binance Futures'taki tüm <quote> paritelerini 24s hacme göre sıralar,
        minimum hacim ve dışlanan bazları (stablecoin'ler vb.) filtreleyip
        en aktif <limit> tanesini döner. Sabit bir coin listesi YOKTUR -
        piyasa hangi coin'lerde hareketliyse bot oraya bakar.
        """
        exclude_base_assets = exclude_base_assets or set()
        tickers = self.data_exchange.fetch_tickers()

        candidates = []
        seen = set()
        for raw_symbol, ticker in tickers.items():
            base_symbol = raw_symbol.split(":")[0]  # 'BTC/USDT:USDT' -> 'BTC/USDT'
            if not base_symbol.endswith(f"/{quote}") or base_symbol in seen:
                continue
            if base_symbol not in self.exchange.markets:
                continue  # emir borsasında (testnet) olmayan pariteyi atla

            base_asset = base_symbol.split("/")[0]
            if base_asset in exclude_base_assets:
                continue

            volume = ticker.get("quoteVolume") or 0
            if volume < min_volume:
                continue

            seen.add(base_symbol)
            candidates.append((base_symbol, volume))

        candidates.sort(key=lambda x: x[1], reverse=True)
        return [symbol for symbol, _ in candidates[:limit]]

    # ------------------------------------------------------------------
    # SENKRONİZASYON (bağlantı koptuktan sonra state kurtarma)
    # ------------------------------------------------------------------
    def fetch_open_positions(self):
        """
        Borsadaki gerçek açık pozisyonları döner - reconnect sonrası DB ile
        karşılaştırılıp 'yetim' pozisyon kalmaması sağlanır.

        ÖNEMLİ: ccxt, sembolü 'BTC/USDT:USDT' formatında döner (sonunda :USDT ile,
        vadeli işlem kontratını belirtmek için). Ama bizim veritabanımızda semboller
        'BTC/USDT' formatında (son ek olmadan) saklanıyor. Bu uyumsuzluk, pozisyon
        eşleştirmesinin HİÇBİR ZAMAN çalışmamasına ve botun her açık pozisyonu birkaç
        saniye içinde "kapanmış" sanmasına neden oluyordu - burada normalize ederek
        tüm sistemde tutarlı format garantiliyoruz.
        """
        positions = self.exchange.fetch_positions()
        result = []
        for p in positions:
            if float(p.get("contracts", 0) or 0) != 0:
                p = dict(p)
                if p.get("symbol") and ":" in p["symbol"]:
                    p["symbol"] = p["symbol"].split(":")[0]
                result.append(p)
        return result
