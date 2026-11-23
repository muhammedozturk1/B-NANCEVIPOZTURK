# 4s Trend Botu — Çıkış Yönetimi: Eski ve Yeni Sistem Karşılaştırması

## Özet

**Kâr geri verme oranı (giveback):** Sadece fiyatın en az +1R'ye gittiği işlemlerde (MFE ≥ 1R) hesaplanır:
`(MFE − sonuç R) / MFE`.
- 0: hiç geri verilmemiş.
- 1: kâr sıfırlanmış.
- 1'in üstü: işlem zararla kapanmış.

Tabloda ortalama gösterilir. Şikâyet edilen durumun doğrudan ölçüsü ise "+1R görüp −0,3R'den kötü kapananların oranı"dır.

**Dönem kırılımları** (eski kurallar ve yeni varsayılan, aynı maliyet ve funding ile):

| | Eski IS 2020–23 | Yeni IS 2020–23 | Eski OOS 2024–26 | Yeni OOS 2024–26 |
|---|---|---|---|---|
| İşlem | 4733 | 4735 | 3723 | 3723 |
| Net R | 390,9 | 385,9 | 202,9 | 202,5 |
| Expectancy (R) | 0,0826 | 0,0815 | 0,0545 | 0,0544 |
| PF | 1,253 | 1,250 | 1,165 | 1,165 |
| MDD (R) | 79,3 | 83,7 | 127,3 | 127,3 |
| Giveback (ortalama) | 0,695 | 0,696 | 0,673 | 0,673 |
| +1R → ≤ −0,3R | %5,6 | %5,5 | %3,9 | %3,9 |
| Portföy getiri / MDD | %161,8 / %15,3 | %162,8 / %15,3 | %66,0 / %19,3 | %66,0 / %19,3 |
| İşlem başı ort. $ (1000 USDT, %1 risk) | 1,76 | 1,77 | 0,92 | 0,92 |

**Denenen kilit varyantlarının kısa özeti** (Net R, IS → OOS; eski kurallar: 391 → 203):
- Şartnamedeki kilit (1→0, 1,5→0,5, 2→1), mark tetik: **35 → 30**. En kötüsü; kâr geri verme sıfırlandı ama büyük trendler kesildi.
- Aynı kilit, 4s kapanış tetik: 311 → 181.
- Kilit + 3×ATR trailing (+1,5R sonrası), saatlik: −18 → −58.
- Aynısı + %33 kısmi TP @ +2R: 1 → −50.
- Kilit + swing ve ATR birlikte (+1R sonrası), 4s kapanış: 198 → 105.
- +2R başabaş + %25 kısmi TP @ +3R: 326 → 163.
- +2R başabaş + swing ve 3×ATR: 230 → 197.
- **+2R başabaş, 4s kapanış (gönderilen varsayılan): 386 → 203.** Eskiyle eşit.

Hiçbir varyant IS'te eski kuralları geçemedi. Bu yüzden OOS'a bakarak seçim yapılmadı.

## Test koşulları (iki sistem için aynı)

**Veri**
- 20 Binance USDⓈ-M coin: BTC, ETH, BNB, XRP, ADA, DOT, LTC, LINK, BCH, XLM, DOGE, UNI, SOL, AVAX, TRX, ETC, ATOM, FIL, LUNA, FTT. Sonradan delist edilenler (LUNA, FTT) de dahil.
- Dönem: 2020-01 – 2026-10. Kaynak data.binance.vision (1 saatlik kline ve gerçek fundingRate).

**Mumlar ve giriş/çıkış kuralı**
- Sinyal 4s mumlarla üretilir. Fiyatın mum içindeki yolu 1 saatlik mumlarla takip edilir.
- Giriş, sinyal mumu kapandıktan sonraki mumun açılışında yapılır.
- Look-ahead yoktur: trailing yalnızca kapanmış mumlarla hesaplanır ve bir sonraki mumdan itibaren geçerli olur.

**Maliyetler**
- Her emirde %0,05 taker komisyonu ve %0,02 kayma.
- Gerçek funding ödemesi: pozitif oranda long öder, short alır.

**Aynı mumda kötü sıra varsayımı**
- Önce stop kontrol edilir. Mum stopun ötesinde açılmışsa çıkış açılış fiyatından yapılır.
- Aynı mumda stop olduysa kâr kilidi veya kısmi TP sayılmaz.
- Mum içi tetikte (mark/last) önce tepe görüldüğü varsayılır. Dip yeni kilit stopuna değiyorsa çıkış kilit stopundan yapılır.

**Portföy kuralları**
- Bakiyenin %1'i risk.
- En fazla 3 pozisyon, aynı yönde en fazla 2, coin başına 1.
- Aynı 4s kapanışında en fazla 1 yeni giriş.
- Başlangıç bakiyesi 1000 USDT.

**Aşırı uyumu önleme**
- Varyantlar sonuçlara bakılmadan önce yazıldı (`scripts/trend_compare/variants.py`).
- Karar yalnızca IS 2020–2023 sonuçlarına göre verildi. OOS 2024–2026 sadece doğrulama için kullanıldı.
- Parametre taraması yapılmadı.

**Eksik: F&G (Korku ve Açgözlülük) geçmişi**
- Bu sunucudan alternative.me'ye erişim engelli olduğu için F&G geçmişi indirilemedi.
- Bu yüzden iki sistem de F&G filtresi kapalı olarak karşılaştırıldı.
- F&G filtresi canlı botta ve panelden yapılan geçmiş testte çalışır.

**Doğrulama:** Yeni motor, eski kurallarla ve aynı 4s veride çalıştırıldığında orijinal `trend.simulate` ile işlem işlem aynı sonucu verdi: 8454 ortak işlem, 0 fark.

## Tanımlar
- **R:** net sonuç / 1R. 1R, giriş fiyatı ile ilk stop arasındaki farktır.
- **Net R:** tüm işlemlerin R toplamı.
- **Expectancy:** işlem başına ortalama net R.
- **PF:** kazançlar toplamı / kayıplar toplamı.
- **MDD (R):** kümülatif R eğrisinde zirveden en büyük düşüş.
- **Portföy MDD (%):** bakiyenin zirveden en büyük düşüşü.
- **Ort. işlem $:** portföy simülasyonunda işlem başına ortalama net USDT.
- **Giveback ratio:** fiyatın en az +1R'ye gittiği işlemlerde (MFE ≥ 1R), görülen en iyi kârın geri verilen kısmı: (MFE − sonuç R) / MFE. Değer 0 ise hiç geri verilmemiştir. 1 ise kâr sıfırlanmıştır. 1'in üstündeyse işlem zararla kapanmıştır.
- **+1R → ≤−0,3R %:** +1R görüp −0,3R veya daha kötü kapanan işlemlerin, +1R görülen işlemlere oranı. Bu, şikâyet edilen durumun ölçüsüdür.

## 1) Ana karşılaştırma

Sistemler:
- **Eski (orijinal kod):** v4.3'teki `trend.simulate`. 4s mum, funding yok.
- **Eski kurallar, aynı maliyet:** aynı kurallar. Yeni motorla, 1s yol ve funding dahil. **Adil kıyas bu satırdır.**
- **Yeni (varsayılan):** gönderilen ayarlar:
  - +2R'de başabaş kilidi (`lock_tiers=2.0:0`)
  - mum kapanışı tetiği
  - 10 mumluk swing trailing

| Dönem | Sistem | İşlem | Net R | Expectancy | PF | MDD (R) | Giveback | +1R→≤−0,3R | Portföy işlem | Portföy getiri | Portföy MDD | Portföy PF | Ort. işlem $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| IS 2020–23 | Eski (orijinal kod) | 4731 | 466,0 | 0,0985 | 1,307 | 76,6 | 0,683 | %5,2 | 922 | %172,5 | %15,2 | 1,252 | 1,87 |
| IS 2020–23 | Eski kurallar, aynı maliyet | 4733 | 390,9 | 0,0826 | 1,253 | 79,3 | 0,695 | %5,6 | 921 | %161,8 | %15,3 | 1,248 | 1,76 |
| IS 2020–23 | **Yeni (varsayılan)** | 4735 | 385,9 | 0,0815 | 1,250 | 83,7 | 0,696 | %5,5 | 921 | %162,8 | %15,3 | 1,248 | 1,77 |
| OOS 2024–26 | Eski (orijinal kod) | 3724 | 226,2 | 0,0607 | 1,185 | 123,4 | 0,667 | %3,7 | 721 | %72,3 | %18,4 | 1,226 | 1,00 |
| OOS 2024–26 | Eski kurallar, aynı maliyet | 3723 | 202,9 | 0,0545 | 1,165 | 127,3 | 0,673 | %3,9 | 720 | %66,0 | %19,3 | 1,208 | 0,92 |
| OOS 2024–26 | **Yeni (varsayılan)** | 3723 | 202,5 | 0,0544 | 1,165 | 127,3 | 0,673 | %3,9 | 720 | %66,0 | %19,3 | 1,208 | 0,92 |
| Tümü | Eski (orijinal kod) | 8455 | 692,2 | 0,0819 | 1,253 | 123,4 | 0,676 | %4,6 | 1642 | %371,3 | %18,4 | 1,238 | 2,26 |
| Tümü | Eski kurallar, aynı maliyet | 8456 | 593,8 | 0,0702 | 1,214 | 127,3 | 0,685 | %4,9 | 1640 | %336,2 | %19,3 | 1,227 | 2,05 |
| Tümü | **Yeni (varsayılan)** | 8458 | 588,4 | 0,0696 | 1,212 | 127,3 | 0,686 | %4,8 | 1640 | %337,8 | %19,3 | 1,227 | 2,06 |

**Eski backtest neden iyimserdi?** Eski kod funding'i saymıyordu. Bu işlem başına ortalama 0,012R maliyet demek. Ayrıca stopları 4s mumla kontrol ediyordu. Doğru hesapla toplam Net R 692 değil 594'tür.

## 2) Kâr kilidi, trailing ve kısmi TP varyantları (kötü sıra + funding dahil)

| Varyant | Dönem | İşlem | Net R | Expectancy | PF | MDD (R) | Giveback | +1R→≤−0,3R | Portföy getiri | Portföy MDD | Ort. işlem $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Şartnamedeki kilit (1→0, 1,5→0,5, 2→1), **mark tetik** | IS | 5092 | 35,4 | 0,0070 | 1,022 | 158,4 | 0,680 | %0,1 | %38,4 | %31,8 | 0,37 |
| | OOS | 3905 | 30,0 | 0,0077 | 1,024 | 141,1 | 0,647 | %0,0 | %21,8 | %20,0 | 0,28 |
| Şartnamedeki kilit, **4s kapanış tetik** | IS | 4794 | 311,3 | 0,0649 | 1,202 | 97,3 | 0,693 | %3,7 | %97,2 | %18,8 | 1,03 |
| | OOS | 3745 | 181,0 | 0,0483 | 1,148 | 125,3 | 0,664 | %2,4 | %60,5 | %17,9 | 0,83 |
| Kilit + 3×ATR trailing (+1,5R sonrası), saatlik | IS | 3426 | −18,2 | −0,0053 | 0,989 | 171,5 | 0,708 | %0,2 | %5,9 | %22,9 | 0,12 |
| | OOS | 2727 | −57,9 | −0,0212 | 0,958 | 176,3 | 0,684 | %0,0 | %0,3 | %21,1 | 0,01 |
| Aynısı + %33 kısmi TP @ +2R | IS | 3426 | 1,3 | 0,0004 | 1,001 | 152,9 | 0,684 | %0,2 | %11,8 | %19,8 | 0,24 |
| | OOS | 2727 | −50,4 | −0,0185 | 0,964 | 162,6 | 0,665 | %0,0 | %1,5 | %18,0 | 0,04 |
| Kilit + swing ve ATR birlikte (+1R sonrası), 4s kapanış | IS | 3326 | 197,5 | 0,0594 | 1,110 | 93,1 | 0,706 | %10,4 | %7,3 | %22,5 | 0,15 |
| | OOS | 2672 | 105,4 | 0,0395 | 1,071 | 128,7 | 0,671 | %8,8 | %40,9 | %13,7 | 1,12 |
| +2R başabaş + %25 kısmi TP @ +3R, mark tetik | IS | 4754 | 326,3 | 0,0686 | 1,211 | 83,5 | 0,692 | %5,1 | %140,6 | %16,1 | 1,52 |
| | OOS | 3729 | 162,7 | 0,0436 | 1,133 | 128,0 | 0,670 | %3,8 | %64,6 | %18,6 | 0,90 |
| +2R başabaş + swing ve 3×ATR (hemen) | IS | 4836 | 229,9 | 0,0475 | 1,149 | 108,6 | 0,701 | %4,0 | %82,7 | %20,2 | 0,86 |
| | OOS | 3759 | 196,5 | 0,0523 | 1,161 | 125,0 | 0,664 | %2,7 | %68,3 | %18,9 | 0,92 |
| **Karşılaştırma: eski kurallar** | IS | 4733 | 390,9 | 0,0826 | 1,253 | 79,3 | 0,695 | %5,6 | %161,8 | %15,3 | 1,76 |
| | OOS | 3723 | 202,9 | 0,0545 | 1,165 | 127,3 | 0,673 | %3,9 | %66,0 | %19,3 | 0,92 |

## 3) Giriş filtreleri (yeni varsayılan çıkışla, tek tek açılarak)

| Filtre | Dönem | İşlem | Net R | Expectancy | PF | MDD (R) | Portföy getiri | Portföy MDD | Portföy PF |
|---|---|---|---|---|---|---|---|---|---|
| Yok (varsayılan) | IS | 4735 | 385,9 | 0,0815 | 1,250 | 83,7 | %162,8 | %15,3 | 1,248 |
| | OOS | 3723 | 202,5 | 0,0544 | 1,165 | 127,3 | %66,0 | %19,3 | 1,208 |
| Hacim ≥ 1,2× | IS | 4180 | 393,4 | 0,0941 | 1,281 | 78,4 | %55,5 | %20,3 | 1,115 |
| | OOS | 3278 | 203,2 | 0,0620 | 1,182 | 120,4 | %53,9 | %21,6 | 1,175 |
| Gövde ≥ 0,5 | IS | 4232 | 371,4 | 0,0878 | 1,267 | 73,8 | %104,8 | %16,8 | 1,195 |
| | OOS | 3388 | 152,5 | 0,0450 | 1,131 | 127,0 | %39,5 | %22,1 | 1,140 |
| Kapanış gücü ≥ 0,6 | IS | 4316 | 357,3 | 0,0828 | 1,250 | 83,0 | %68,0 | %23,3 | 1,134 |
| | OOS | 3467 | 184,4 | 0,0532 | 1,158 | 117,3 | %45,2 | %21,4 | 1,158 |
| Üçü birlikte | IS | 3532 | 381,9 | 0,1081 | 1,319 | 71,0 | %95,7 | %18,7 | 1,197 |
| | OOS | 2861 | 172,3 | 0,0602 | 1,171 | 108,1 | %34,8 | %22,5 | 1,128 |
| Korelasyon ≤ 0,8 (60 mum) | IS | (portföy) | | | | | %226,1 | %18,2 | 1,327 |
| | OOS | (portföy) | | | | | %49,1 | %17,6 | 1,166 |

## Sonuçlar (dürüst değerlendirme)

1. **+1,7R / +2,2R'den zararla kapanma nadir ama gerçek.**
   - +1R görülen işlemlerin yalnızca %4–6'sı −0,3R'den kötü kapanıyor.
   - Sistemin kârı az sayıdaki büyük trendden geliyor: +3R üstü 217 işlem toplam +1301R getirdi, oysa tüm dönemin toplamı +594R.
   - Ortalama kazanç +1,15R, ortalama kayıp −0,50R.
2. **Şartnamedeki sıkı kâr kilidi bu veride getiriyi yok etti.**
   - Mark fiyatı tetikli kilit: Net R IS'te 391'den 35'e, OOS'ta 203'ten 30'a düştü. Portföy MDD %15'ten %32'ye çıktı.
   - Nedeni: +1R'de stop 0R'ye çekilince, sonradan büyük trende dönen işlemler normal geri çekilmede (1R = 3×ATR) kapanıyor.
   - "Kâr geri verme" azalıyor (+1R→zarar %0), ama toplam kâr daha da hızlı azalıyor.
3. **Önceden yazılan hiçbir kilit, trailing veya kısmi TP varyantı IS'te eski kuralları geçemedi.**
   - Bu yüzden OOS'a göre seçim yapılmadı.
   - En yakın sonuç +2R'de 4s kapanış tetikli başabaş kilidi: sonuçlar eskiyle neredeyse aynı (Tümü: 588 R'ye karşı 594 R).
   - Varsayılan olarak bu gönderildi: performansı değiştirmeden +2R'yi geçen işlemlere başabaş koruması ekler. Tamamen eski davranış için `lock_tiers` boş bırakılabilir.
4. **Filtreler tutarlı bir iyileşme vermedi. Hepsi varsayılan olarak kapalı.**
   - Hacim filtresi ve "üçü birlikte" işlem başı beklentiyi iki dönemde de artırdı. Ama işlem sayısı azaldığı için portföy getirisi düştü ve MDD arttı.
   - Korelasyon sınırı IS'te iyi, OOS'ta kötü sonuç verdi; bu tutarsız.
5. **Eski backtest iyimserdi.** Funding ve mum içi yol eklenince toplam Net R 692'den 594'e indi (yaklaşık %14). Panelde gösterilecek sonuçlar artık bu maliyetleri içerir.

## Sınırlamalar
- F&G filtresi bu karşılaştırmada yok (veri indirilemedi).
- Mark fiyatı yerine 1s mumun tepe ve dibi kullanıldı. Mark fiyatı son fiyattan daha yumuşaktır, bu yüzden mark tetikli sonuçlar biraz kötümser olabilir.
- 20 coin ile test edildi (README'deki 40 coinlik test ayrıdır).
- Geçmiş performans gelecek için garanti değildir.
