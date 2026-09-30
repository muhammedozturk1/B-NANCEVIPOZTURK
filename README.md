# Kripto Trading Bot v2 — AI Filtreli, "Küçük ama Net Kazanç" Modu

## v2'de ne değişti?

**Düzeltilen hatalar**
- Sinyaller artık sadece **kapanmış mumlarla** hesaplanıyor. Eskiden oluşmakta olan mum kullanılıyordu ve sinyaller kayboluyordu.
- Pivotlar önceki **tam günden** hesaplanıyor. Eski kod tek mumdan hesapladığı için sürekli sahte "long" sinyali üretiyordu.
- VWAP her UTC gününde **sıfırlanıyor**.
- **Komisyon ve kayma** her hesapta var: backtest, AI etiketi ve PnL. Kapanan işlemin gerçek net PnL'i borsadan okunuyor.
- SL/TP **gerçek dolum fiyatına** göre konuyor.
- Stop taşınırken önce yeni stop konuyor, sonra eskisi iptal ediliyor. Böylece pozisyon arada korumasız kalmıyor.
- Dashboard ve bot aynı işlemi iki kez kasaya işleyemiyor.

**Strateji / çıkış**
- "0.5R'de başa-baş, %40 geri çekilmede kapat" sistemi kaldırıldı. Kazançları kırpıp zararları tam bırakıyordu.
- Tek hedef: **1.2R (scalp) / 1.5R (day)**. Ayrıca bir zaman stopu var: scalp 3 saat, day 8 saat.
- 1 dakikalık scalp kaldırıldı. Scalp artık 5m/1h, day 15m/4h zaman dilimlerinde çalışıyor.
- Birbiriyle çelişen oylama sistemi yerine 3 net kurulum var: `trend_pullback`, `range_reversion`, `liquidity_sweep`.

**Risk**
- İşlem başına risk kasanın **%1**'i. Eski ayarda sabit 5$/10$ vardı, bu 100$ kasada %5-10 risk demekti.
- Kill-switch tekrar açık: günlük %5 zarar veya art arda 4 kayıpta motor 4 saat duruyor.
- Toplam açık risk en fazla %4, aynı yöndeki (tüm altcoinler dahil) risk en fazla %3.
- Kaldıraç otomatik: marja sığan en düşük kaldıraç seçiliyor, üst sınır 10x. Likidasyon fiyatı stoptan en az 3 kat uzakta tutuluyor.

**Yapay zeka**
- Her aday sinyal için model şu soruya cevap veriyor: "Bu işlem komisyon sonrası kâr eder mi?" (Gradient Boosting)
- Model gerçek geçmiş veride eğitiliyor ve **hiç görmediği bir test döneminde** sınanıyor.
- **Testi geçemeyen model onay almaz. Onaysız modelle bot o motorda işlem AÇMAZ** (`AI_MODE=required`).
- Açılan her işlemin özellikleri veritabanına kaydediliyor (`features_json`). İleride bu kayıtlarla gerçek işlemlerden yeniden eğitim yapılabilir.

---

## ADIM A: Modeli eğit (kendi bilgisayarında)

İnternet bağlantısı gerekir, API key gerekmez. Gerçek Binance verisi indirilir.

```bash
pip install -r requirements.txt
python -m backend.ai.train --engine scalp --days 365
python -m backend.ai.train --engine day --days 730
```

İlk çalıştırma veri indirdiği için 10-30 dakika sürebilir. Sonraki çalıştırmalar `data/` klasöründeki önbelleği kullanır.

## ADIM B: Raporu oku

Ekranda ve `models/<motor>_report.json` dosyasında şunlar yazar:

- **AUC**: 0.50 yazı-tura demektir. 0.55'in üstü anlamlı bir sinyal var demektir. 0.70'in üstü şüphelidir, bana bildir.
- **AI'sız / AI filtreli**: test dönemindeki işlem sayısı, kazanma oranı, ortalama R (komisyon sonrası), profit factor, maksimum düşüş.
- **✅ ONAYLANDI** veya **❌ onaylanmadı** sonucu.

"Onaylanmadı" çıkarsa bot o motorla işlem açmaz. Bu bir hata değil, koruma: stratejinin o piyasada avantajı yok demektir, para kaybetmeden öğrenmiş olursun.

## ADIM C: Modeli GitHub'a yükle ve deploy et

Render modeli repodan okur. Onaylanan modelleri commit'le:

```bash
git add models/ && git commit -m "AI modelleri" && git push
```

## ADIM D: En az 2-4 hafta testnette izle

Testnet sonuçlarını raporla karşılaştır. Kazanma oranı ve ortalama R, test dönemine yakın olmalı. Çok daha kötüyse canlıya geçme.

## Bakım

- Modeli **ayda bir** yeniden eğit, çünkü piyasa rejimi değişiyor.
- `config.py` içinde `EXIT_PARAMS` veya `ENGINE_SETUPS` değiştirirsen **mutlaka yeniden eğit**. Model eski kurallara göre eğitilmişse yanlış karar verir.
- `AI_MIN_PROBABILITY` ile daha seçici olabilirsin: daha az ama daha kaliteli işlem.

## ⚠️ Dürüst uyarılar

- Backtest geçmişi ölçer, geleceği garanti etmez.
- Scalp'ta komisyon ve kayma işlem başına yaklaşık **0.2R** tutar. Avantajı olmayan bir sinyal bu yüzden kesin kaybettirir. AI filtresinin görevi tam olarak bunu elemek.
- Eğitimde bugünün en hacimli coinleri kullanılıyor. Bu durum sonuçları biraz iyimser gösterebilir (hayatta kalma yanılgısı).
- Gerçek paraya geçmeden önce kaybetmeyi göze alabileceğin küçük bir tutarla başla.

---

# İlk Kurulum Rehberi (Faz 1 - hâlâ geçerli)

Bu rehber, hiç deneyimin olmadığını varsayarak yazıldı. Her adımı sırayla, atlamadan yap.

---

## 📌 ŞU ANDA NEREDEYİZ?

Faz 1'de sadece **altyapıyı** kurduk:
- Proje klasör yapısı
- Veritabanı şeması
- Binance Testnet bağlantısı
- İlk 2 teknik gösterge (EMA, VWAP)

**Henüz gerçek/testnet işlem açılmıyor.** Bu fazın amacı: "bağlantılar çalışıyor mu?" diye test etmek.

---

## ADIM 1: GitHub Reposu Oluştur

1. https://github.com adresine git, hesabın yoksa ücretsiz aç.
2. Sağ üstte **+** işaretine tıkla → **New repository**.
3. İsim ver (örn: `crypto-bot`), **Private** seç (herkese açık olmasın, API key'lerin olmasa bile güvenlik için).
4. **Create repository** butonuna bas.
5. Oluşan boş repo sayfasında "…or push an existing repository" kısmındaki komutları not al (birazdan kullanacağız).

Bana repo adını/linkini verirsen, dosyaları oraya nasıl yükleyeceğini de adım adım gösteririm.

---

## ADIM 2: Binance Testnet Hesabı ve API Key

**Dikkat:** Bu gerçek Binance hesabından FARKLI bir sistemdir, sahte parayla çalışır.

1. https://testnet.binancefuture.com adresine git.
2. Sağ üstten GitHub hesabınla giriş yap (Binance testnet, GitHub ile giriş istiyor - normal).
3. Giriş yaptıktan sonra sana otomatik olarak sahte USDT bakiyesi verilir (genelde 10.000-15.000 arası).
4. Üst menüden **API Key** bölümüne git (bazen "API Management" olarak geçer).
5. Yeni bir API key oluştur, **Key** ve **Secret** değerlerini kopyala.
   - ⚠️ Secret sadece bir kere gösterilir, kaybedersen yeni key oluşturman gerekir.

---

## ADIM 3: Telegram Bot Oluştur

1. Telegram uygulamasında **@BotFather** adlı hesabı bul (arama kutusuna yaz).
2. `/newbot` komutunu gönder.
3. Bota bir isim ver (örn: "Kripto Bot Bildirim").
4. Bir kullanıcı adı ver (sonu "bot" ile bitmeli, örn: `kriptobot_bildirim_bot`).
5. BotFather sana bir **token** verecek (şuna benzer: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`). Kopyala.
6. Şimdi kendi **Chat ID**'ni bulman lazım:
   - Oluşturduğun bota Telegram'dan git, herhangi bir mesaj gönder (örn: "merhaba").
   - Tarayıcıda şu adrese git (TOKEN yerine kendi token'ını yaz):
     `https://api.telegram.org/botTOKEN/getUpdates`
   - Açılan sayfada `"chat":{"id":123456789,...}` gibi bir kısım göreceksin. O sayı senin Chat ID'n.

---

## ADIM 4: Dosyaları Bilgisayarına İndir ve .env Doldur

1. Aşağıda paylaştığım ZIP dosyasını indir, bir klasöre çıkart (unzip).
2. İçindeki `.env.example` dosyasının bir kopyasını oluştur, adını `.env` yap (nokta ile başlıyor, dikkat).
3. `.env` dosyasını bir metin editörüyle aç (Not Defteri yeterli) ve şunları doldur:
   - `BINANCE_API_KEY` → Adım 2'deki key
   - `BINANCE_API_SECRET` → Adım 2'deki secret
   - `TELEGRAM_BOT_TOKEN` → Adım 3'teki token
   - `TELEGRAM_CHAT_ID` → Adım 3'teki chat id
4. Kaydet.

---

## ADIM 5: GitHub'a Yükle

Bilgisayarında terminal/komut satırı aç (Windows'ta "cmd" veya "PowerShell", Mac'te "Terminal"), indirdiğin klasöre girip şu komutları sırayla çalıştır:

```bash
git init
git add .
git commit -m "Faz 1: temel altyapı"
git branch -M main
git remote add origin <GITHUB_REPO_LINKIN>
git push -u origin main
```

`<GITHUB_REPO_LINKIN>` yerine Adım 1'de oluşturduğun reponun linkini yaz (örn: `https://github.com/kullaniciadi/crypto-bot.git`).

**Not:** `.env` dosyası `.gitignore` sayesinde otomatik olarak yüklenmeyecek — bu bilinçli bir güvenlik önlemi, API key'lerin GitHub'da görünmemeli.

---

## ADIM 6: Lokal Test (Opsiyonel ama Önerilir)

Eğer bilgisayarında Python varsa (yoksa bu adımı atlayıp direkt Render'a geçebiliriz):

```bash
pip install -r requirements.txt
python -m backend.main
```

Ekranda şöyle bir çıktı görmen lazım:
```
✅ Veritabanı tabloları hazır.
✅ Binance bağlantısı OK. Testnet USDT bakiyesi: 15000.0
📊 BTC/USDT | EMA: long_weak | VWAP: neutral
📊 ETH/USDT | EMA: short_weak | VWAP: long
```

Bu çıktıyı görürsen Faz 1 başarılı demektir.

---

## ⏭️ SIRADAKİ FAZLAR (henüz yapılmadı)

- **Faz 2:** 3 motor (scalp/day/swing), confluence sinyal mantığı, kalan 4 teknik (SMC, likidite bölgeleri, korku-açgözlülük, destek/direnç)
- **Faz 3:** Risk yönetimi otomasyonu (breakeven, trailing/momentum kaybı kapama, kill-switch, korelasyon filtresi, haber filtresi)
- **Faz 4:** Telegram bildirim akışı (açılış + reply-thread güncellemeleri + heartbeat + haftalık özet)
- **Faz 5:** Dashboard (React + mum grafiği)
- **Faz 6:** Render'a tam deploy + canlı testnet çalıştırma

Faz 1'i test edip bana sonucu (çalıştı mı, hata aldın mı) bildirdiğinde Faz 2'ye geçeceğim.
