"""
BOT SIFIRLAMA SCRIPTİ

Bu script:
  1) Borsadaki (testnet) açık yönlü pozisyonları kapatmayı DENER
     (borsaya bağlanılamazsa bu adımı atlar, sıfırlamaya devam eder)
  2) Tüm işlem geçmişini, fonlama pozisyonlarını, motor durumlarını, günlük PnL kayıtlarını siler
  3) Sanal kasayı STARTING_BALANCE'a (varsayılan $100) sıfırlar

Kullanım (Render > crypto-bot-engine > Shell):
    python reset_bot.py

DİKKAT: Bu işlem GERİ ALINAMAZ.
"""
from backend.db.database import get_session, init_db
from backend.db.models import Trade, EngineState, DailyPnL, BotCapital, FundingPosition, TradeStatus
from backend import config

print("=== BOT SIFIRLAMA BAŞLIYOR ===")
init_db()

# 1) Açık yönlü pozisyon varsa borsada kapatmayı dene.
#    (Eski sürüm borsaya bağlanamazsa hata verip HİÇBİR ŞEYİ silmeden duruyordu.)
session = get_session()
try:
    open_directional = session.query(Trade).filter(Trade.status == TradeStatus.OPEN).count()
finally:
    session.close()

if open_directional == 0:
    print("Borsada kapatılacak yönlü pozisyon yok (fonlama pozisyonları sanal, borsada değil).")
else:
    try:
        from backend.exchange.binance_client import BinanceClient
        client = BinanceClient()
        for p in client.fetch_open_positions():
            symbol = p["symbol"]
            side = "buy" if p["side"] == "long" else "sell"
            amount = abs(float(p["contracts"]))
            print(f"Kapatılıyor: {symbol} {side} {amount}")
            try:
                client.cancel_all_algo_orders(symbol)
                client.close_position(symbol, side, amount)
                print(f"✅ Kapandı: {symbol}")
            except Exception as e:
                print(f"❌ {symbol} kapatılamadı: {e}")
    except Exception as e:
        print(f"⚠️ Borsaya bağlanılamadı, bu adım atlandı: {e}")

# 2) Veritabanını temizle
session = get_session()
try:
    counts = {
        "İşlem": session.query(Trade).delete(),
        "Fonlama pozisyonu": session.query(FundingPosition).delete(),
        "Motor durumu": session.query(EngineState).delete(),
        "Günlük K/Z": session.query(DailyPnL).delete(),
        "Kasa kaydı": session.query(BotCapital).delete(),
    }
    session.add(BotCapital(balance=config.STARTING_BALANCE))
    session.commit()
    print("Silindi -> " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
finally:
    session.close()

print(f"=== SIFIRLAMA TAMAMLANDI - kasa {config.STARTING_BALANCE:.2f}$ ===")
