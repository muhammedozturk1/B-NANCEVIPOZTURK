def _place_algo_stop_order(self, symbol: str, entry_side: str, amount: float,
                                trigger_price: float, order_type: str):
        """
        order_type: 'STOP_MARKET' veya 'TAKE_PROFIT_MARKET'

        ÖNEMLİ DEĞİŞİKLİK (Kâr Koruma):
        Take-profit artık TAKE_PROFIT_MARKET olarak gönderiliyor (eskiden LIMIT'ti).
        Sebep: LIMIT TP emri, fiyat TP seviyesine DEĞDİĞİ anda tetiklenmiyordu;
        fiyatın o seviyede kalması gerekiyordu. Fiyat TP1'e değip anında geri
        dönerse TP hiç gerçekleşmiyor, sonra fiyat ters yöne gidip stop patlıyordu.
        Bu yüzden tabloda "7 TP1 / 8 Stop" gibi dengesiz bir dağılım görülüyordu.
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
            "workingType": "CONTRACT_PRICE",
        }

        # TAKE_PROFIT_MARKET için ekstra fiyat/timeInForce GEREKMEZ (MARKET emir).
        # STOP_MARKET için de gerekmez. İkisi de doğrudan tetiklendiğinde
        # market olarak çalışır.

        return self._signed_algo_request("POST", "/fapi/v1/algoOrder", params)
