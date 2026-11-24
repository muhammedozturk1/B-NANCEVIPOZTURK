# SMC Scalper — Scalp Şartnamesi Geçmiş Testi

## Kurallar (koddaki karşılığı: `backend/smc/strategy/scalp.py`)

**Genel**
- Pariteler: BTCUSDT ve ETHUSDT. Yön 5 dakikalık, giriş 1 dakikalık mumlarla belirlenir.
- Sabit 2 USDT risk; miktar = 2 / stop mesafesi. Kaldıraç 10x, aynı anda en fazla 1 pozisyon.

**Yön (5 dakika)**
- Fiyat EMA20'nin üstündeyse sadece LONG, altındaysa sadece SHORT.
- Fiyat EMA'ya %0,05'ten yakınsa ("yapışık") işlem açılmaz.

**Giriş (1 dakika; hepsi aynı anda)**
- Fiyat son 6 mumun tepesini (LONG) ya da dibini (SHORT) kırar.
- Kırılım mumunun hacmi, son 15 mumun ortalamasının en az 1,8 katıdır.
- Kırılım mumunun gövdesi, mum boyunun en az %55'idir.
- RSI(5): LONG için 78'in altında, SHORT için 22'nin üstünde.

**Çıkış**
- Stop: kırılım mumunun %0,01 altı/üstü. Stop mesafesi %0,38'i geçerse işlem açılmaz.
- TP: stop mesafesinin 1,4 katı.
- Fiyat 0,7R'ye ulaşınca stop giriş fiyatına çekilir.
- En fazla 6 dakika tutulur; süre dolunca piyasa emriyle kapatılır.
- Ters yönde güçlü ve hacimli mum gelirse hemen kapatılır.

**Ek filtreler**
- 01:30–05:30 UTC arası işlem açılmaz.
- Spread %0,02'nin üstündeyse işlem açılmaz.
- Son 2 mumda tek fitil, mum boyunun %60'ından uzunsa işlem açılmaz.

## Test koşulları
- **Veri:** Binance USDⓈ-M 1 dakikalık mumlar, 1 Mayıs – 14 Kasım 2026 (data.binance.vision).
- **Kararlar:** look-ahead yok; karar, kırılım mumu kapandıktan sonra verilir. Giriş bir sonraki mumun açılışında yapılır.
- **Aynı mumda kötü sıra varsayımı:**
  - Önce stop kontrol edilir; mum stopun ötesinde açılmışsa çıkış açılış fiyatından yapılır.
  - Ardından TP kontrol edilir.
  - Başabaş stopu bir sonraki mumdan itibaren geçerlidir.
- **Maliyet:** taker %0,05 (giriş ve çıkış) + kayma %0,02.
- **Bakiye:** 200 USDT. 10x teminatı kasayı aşan işlemler açılmaz (şartname: "risk tam 2 USDT değilse açma").
- **Kod:** bot ile aynı fonksiyonlar. Script: `scripts/scalp_backtest.py`.

## Sonuçlar

| Senaryo | İşlem | Kazanma | Net | İşlem başı | PF |
|---|---|---|---|---|---|
| Komisyon ve kayma yok (kuralların kendisi) | 3.425 | %36,7 | −8$ | ≈0 R | 0,997 |
| Ucuz maliyet (%0,02, kaymasız) | 3.425 | %28,6 | −3.708$ | −0,54 R | 0,28 |
| Gerçekçi maliyet, sınırsız teminat | 3.386 | %11,6 | −9.462$ | −1,40 R | 0,04 |
| **Gerçekçi maliyet, 200 USDT kasa** | 117 | %19,7 | **200$ → 53$** | −0,63 R | 0,16 |

**Ek gözlemler**
- Ortalama stop mesafesi %0,12–0,25 olduğu için 2$ risk ~900–2.000$ nominal demektir. Giriş + çıkış komisyonu tek başına 1–2$ eder, yani riskin kendisi kadar.
- Komisyonsuz bile kuralların avantajı yoktur; maliyet eklenince her ay zarardır.
- 200 USDT kasa ilk iki ayda 53 USDT'ye iner. Ardından 10x teminat kasayı aştığı için işlem açılamaz.
- Çıkış nedenleri (gerçekçi maliyet): 6 dk süre dolumu 1.162, stop 1.126, TP 471, ters mum 291, başabaş 277.

## Sınırlamalar
- Testnet/demo emir defteri gerçek piyasadan farklı olabilir (spread, derinlik, dolum).
- Mark fiyatı yerine 1 dakikalık mumun tepe/dibi kullanıldı.
- 6,5 aylık tek dönem test edildi.
