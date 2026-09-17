# Kripto Trading Bot - Kurulum Rehberi (FAZ 1)

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
