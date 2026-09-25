# Canlı Alarm — Furkanla Katla

Sürekli çalışan canlı alarm botu. Her 30 saniyede 1 dakikalık mumlara bakar; sadece şu
durumlarda Telegram'a yazar:

- **Hacim patlaması:** son 5 dakikanın hacmi önceki saatin ortalamasının 5 katı
- **Ani sıçrama:** son 5 dakikada %3+ (kripto %2+)
- **Gün içi zirve kırılımı:** hacimle birlikte günün zirvesi aşıldı

Aynı hisseye 45 dakika tekrar yazmaz (fiyat son alarmdan %5 daha yükselirse yazar), saatte en
fazla 15 mesaj. İzlenenler: ABD hisseleri + günün en çok yükselen / en aktif / küçük şirket
listeleri (0,5-20 $, ön piyasa 11:00 TR'den itibaren), kripto (7/24), BIST (~15 dk gecikmeli).
Alarm çıkınca Gemini o hisse için internette haber arar (anahtar varsa).

## Sinyal filtreleri (`sinyal_filtreleri.py`)
Alarm bulununca mesaj hemen gitmez; o hissenin 5 dakikalık mumlarıyla filtreden geçer. Geçemezse
gönderilmez, nedeni log'a `ATLANDI XYZ: ...` diye yazılır. Kurallar:
- Kırılım mumu hacimli olmalı (son 20 mumun 1,5 katı), kapanış kırılan seviyenin üstünde olmalı
- Fiyat VWAP üstünde, ama VWAP'tan 2 ATR'den fazla uzaklaşmamış olmalı
- Tepede hacimli doji olmamalı; risk %6'dan büyük olmamalı; TP1'den önce direnç olmamalı
- Günlük RVOL: ABD ≥ 5x, BIST ≥ 3x (kripto'da uygulanmaz)

**Zorunlu / esnek kurallar:** Kırılımın fitilde kalması, fiyatın VWAP altında olması, tepede hacimli doji,
risk > %6 ya da 0,10 $ altı fiyat **zorunlu** kurallardır; biri bile bozulursa mesaj gitmez. Günlük RVOL,
mum hacmi, VWAP'tan uzaklık ve TP1 önünde direnç **esnek** kurallardır: yalnız biri bozuksa sinyal
`🟡 AL · ORTA` etiketiyle (altında `⚠️ Dikkat: ...`) gider, hepsi tamamsa `🟢 AL · GÜÇLÜ`. `LIVE_MAX_SOFT=0` → her kural zorunlu.

Geçen sinyal kısa bir **AL** mesajı olarak gider; giriş / kademe / stop filtrenin seviyeleridir
(TP'ler riskin 1,5 / 2,5 / 4 katı). Giriş aralığı: fiyatın riskin 1/4'ü altı (kırılan seviyenin altına
inmeden) ile 0,15'i üstü; üst sınırın üstünde alınırsa TP1'in risk/ödülü 1:1'in altına düşer.

```
🟢 $SRFM AL · GÜÇLÜ

🔹 Giriş: 1.08 – 1.10$
🛑 Stop: 1.03$ (%-5.0)
🎯 Kademeler: 1.17$ → 1.23$ → 1.31$

⚡ Zirve kırıldı · hacim 5.8x · 5 dk %+3.3 · gün %+19.8
📰 Haber: ...
💡 TP1'de 1/3 sat, stopu girişe çek
```
Haber: ABD hisselerinde `FINNHUB_KEY` varsa son 24 saatin başlığı (`📰 Haber yok (dikkat)` da olabilir),
yoksa Gemini araştırır; Gemini kotası dolarsa Google Haberler'in son 24 saatlik başlıkları eklenir.

**Çıkış takibi:** Gönderilen sinyal 6 saat izlenir (5 dk'da bir). Stop çalışırsa, sahte kırılım,
hacimsiz yükseliş, hacimli doji, VWAP altı kapanış ya da büyük kırmızı mum olursa kısa bir
`🔴 $XYZ SAT — sahte kırılım` (hacim sönünce `🟠 YARISINI SAT`, dojide `🟠 KÂR AL`) mesajı gider. TP1 görülünce stop girişe çekilir, sonra iz süren stop.

Ayarlar: `LIVE_FILTERS=0` (filtreleri kapatır, eski davranış) · `LIVE_MAX_SOFT` (1) · `LIVE_RVOL_US` (5) · `LIVE_RVOL_BIST` (3) ·
`LIVE_TRACK_HOURS` (6) · `LIVE_EXIT_CHECK_SEC` (300) · `LIVE_KASA` (ör. 1000 → adet önerisi).
Eşikleri değiştirmek için `sinyal_filtreleri.py` başındaki sabitler düzenlenir.

## KAP bildirimleri, takvim ve Telegram komutları
- **📢 KAP:** Takip listesindeki BIST şirketlerinin önemli bildirimleri (özel durum, finansal rapor, kâr payı,
  sermaye artırımı, geri alım, yeni iş ilişkisi, ihale…) 2 dakikada bir kontrol edilir ve link ile gönderilir.
  Rutin bildirimler (genel kurul ilanı vb.) gönderilmez. `KAP_ALERTS=0` kapatır, `KAP_MAX_PER_HOUR` (10).
- **📅 Takvim:** Her iş günü 09:00'da önümüzdeki 3 günün bilanço ve temettü (hak düşüm) tarihleri
  (ABD + BIST takip listesi). Olay yoksa mesaj gitmez. `TAKVIM=0` kapatır, `TAKVIM_HOUR`, `TAKVIM_DAYS`.
- **🤖 Komutlar** (gruba yaz, ~30 sn içinde cevap gelir): `/durum` · `/fiyat THYAO` (ya da AAPL, BTC) ·
  `/takvim` · `/sonuc` · `/yardim`. Sadece bu gruptan gelen komutlar işlenir. `TG_COMMANDS=0` kapatır.

## Güvenilirlik
- **Tatiller:** BIST ve ABD tatilleri/yarım günleri tanımlı (`bot/tatil.py`); tatilde o piyasa taranmaz.
  Yeni yılın bayram tarihleri `EXTRA_HOLIDAYS_BIST="2027-03-09,..."` ile koda dokunmadan eklenebilir.
- **Bayat veri koruması:** son mum 5 dk'dan (BIST 25 dk) eskiyse alarm verilmez (dünün seansı / tatil).
- **Durum kaydı:** `state/live.json` 20 dk'da bir GitHub'a kaydedilir (`LIVE_STATE_PUSH_MIN`).
- Takvim arka planda hazırlanır (alarm döngüsü durmaz); Telegram hatası turu bozmaz.

## Canlı sonuç raporu
Kapanan her canlı sinyalin sonucu (çıkış ya da 6 saat dolması) kaydedilir. Her gün 23:30'da
`⚡ CANLI ALARM — BUGÜNÜN SONUÇLARI` (kazanma oranı, ortalama, en iyi/kötü) gelir; `/sonuc` son 7 gün.

## Kurulum
1. Settings → Secrets and variables → Actions: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   isteğe bağlı `GEMINI_API_KEY` ve `FINNHUB_KEY` (finnhub.io'dan ücretsiz).
2. Settings → Actions → General → Workflow permissions: **Read and write permissions**.
3. Actions sekmesinde workflow'ları etkinleştir. "Canlı alarm" bir kez başlayınca ~5 saat 40 dk
   çalışır ve bitince bir sonrakini kendisi başlatır (kesintisiz). Zincir koparsa yedek zamanlayıcı
   (her saat :13 ve :43) yeniden başlatır. Hemen başlatmak için **Run workflow**.

## Ayarlar (workflow `env:` kısmına)
`LIVE_MARKETS` (us,kripto,bist) · `LIVE_POLL_SECONDS` (30) · `LIVE_COOLDOWN_MIN` (45) ·
`LIVE_REARM_PCT` (0.05) · `LIVE_MAX_PER_HOUR` (15) · `LIVE_JUMP_PCT` · `LIVE_VOLUME_MULT` (5) ·
`LIVE_MIN_PRICE` (0.5) · `LIVE_MAX_PRICE` (20) · `LIVE_NEWS_PER_HOUR` (6) · `SIGNATURE`

Testler: `python -m unittest discover -s tests -v`
