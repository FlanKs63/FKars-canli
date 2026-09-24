"""
KRS_bot için sinyal filtreleri — videolardan çıkarılan kurallar koda dökülmüş hâli.
Girdi: gün içi mum verisi (pandas DataFrame; kolonlar Open, High, Low, Close, Volume;
index = zaman). vwap() her günü kendisi ayırır.

Kullanım (botun "ANİ HAREKET" mesajını göndermeden hemen önce):
    from sinyal_filtreleri import sinyal_degerlendir, gunluk_rvol
    rv = gunluk_rvol(bugun_hacim, ort_30g_hacim, seans_acilistan_beri_dakika)
    k = sinyal_degerlendir(df_5dk, kirilan_seviye=gun_ici_zirve,
                           onceki_gun_zirvesi=pdh, gunluk_rvol_degeri=rv, kasa=1000)
    if not k['gonder']:
        return                      # sinyali atla (k['neden'] loga yazılabilir)
    mesaj += k['mesaj_ek']          # giriş / kademe / stop satırlarını bununla değiştir

Kaynak kurallar:
  [F]  Sahte kırılım videosu          [V]  VWAP videosu
  [D]  Hacimli doji videosu           [T]  1 dk day-trade videosu
  [B]  Bootcamp risk yönetimi         [A]  ATR ile stop videosu
  [R]  R / risk-ödül aracı videosu    [K]  Kademeli çıkış videosu
  [P]  Ross Cameron penny stock (mikro pullback, fiyat filtresi, büyük kırmızı mum)
  [RV] Ross Cameron relative volume (günlük RVOL ≥ 5)
  [G]  Gap / window (TP1'e kadar önü açık mı)
"""
import pandas as pd
import numpy as np

# ---- Ayarlar (backtest sonuçlarına göre ince ayar yapılacak) ----
MIN_RVOL_KIRILIM = 1.5      # [F][V] kırılım mumu hacmi, son 20 mum ortalamasının en az bu katı
MIN_GUNLUK_RVOL = 5.0       # [RV] bugünkü hacim normal günün en az 5 katı (saate göre oranlı)
MAX_VWAP_UZAKLIK_ATR = 2.0  # [V] fiyat VWAP'tan bu kadar ATR uzaksa kovalama
MIN_RR = 1.5                # [A][R][B] TP1 en az riskin 1.5 katı (şu an botta ~0.66!)
STOP_ATR_TAMPON = 0.5       # [V] stop, destek seviyesinin bu kadar ATR altına
DOJI_GOVDE_ORANI = 0.10     # [D] gövde / (tepe-dip) ≤ %10 ise doji
DOJI_HACIM_KAT = 1.5        # [D] doji hacmi ortalamanın bu katından büyükse "hacimli"
MIN_FIYAT = 0.10            # [P] 10 sentin altı çok riskli / manipülatif
MAX_RISK_PCT = 6.0          # risk bundan büyükse sinyal gönderme
KASA_RISK_PCT = 1.0         # [R][B] her işlemde kasanın %1'i risk (1R)


def atr(df, n=14):
    h, l, c = df.High, df.Low, df.Close.shift()
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=3).mean()


def vwap(df):
    """[V] Günlük VWAP, her seansta sıfırlanır. Kaynak: (H+L+C)/3."""
    tp = (df.High + df.Low + df.Close) / 3
    gun = df.index.date
    pv = (tp * df.Volume).groupby(gun).cumsum()
    v = df.Volume.groupby(gun).cumsum()
    return pv / v.replace(0, np.nan)


def rvol(df, n=20):
    """Son mumun hacmi / önceki n mumun ortalama hacmi."""
    ort = df.Volume.iloc[-n - 1:-1].mean()
    return float(df.Volume.iloc[-1] / ort) if ort else 0.0


def gunluk_rvol(bugun_hacim, ort_gunluk_hacim, gecen_dakika, seans_dk=390):
    """[RV] Bugünkü hacmi saate göre oranlayıp 30 günlük ortalamayla kıyaslar.
    Seansın 1/4'ü geçtiyse normal günün 1/4'ü beklenir."""
    beklenen = ort_gunluk_hacim * max(gecen_dakika, 15) / seans_dk
    return bugun_hacim / beklenen if beklenen else 0.0


def hacimli_doji(df, i=-1, n=20):
    """[D] i. mum hacimli bir doji mi? (trend tepesinde dönüş uyarısı)"""
    b = df.iloc[i]
    aralik = b.High - b.Low
    if aralik <= 0:
        return False
    govde = abs(b.Close - b.Open)
    ort = df.Volume.iloc[i - n:i].mean() if len(df) > n else df.Volume.mean()
    return govde / aralik <= DOJI_GOVDE_ORANI and b.Volume >= DOJI_HACIM_KAT * ort


def hacim_sonuyor(df, bar=3):
    """[F] Kırılımdan sonra fiyat çıkarken hacim her mumda azalıyorsa sahte kırılım riski."""
    v = df.Volume.iloc[-bar:]
    return len(v) == bar and all(v.diff().dropna() < 0)


def mikro_pullback(df, bak=6):
    """[P] Yükselişten sonra kısa geri çekilme, ardından 'mum üstü mum':
    son mum bir önceki (geri çekilen) mumun tepesini aşıp üstünde kapattı mı?
    Döner: (bool, pullback_dibi)"""
    if len(df) < bak + 2:
        return False, None
    son, onceki = df.iloc[-1], df.iloc[-2]
    geri_cekilme = onceki.Close < onceki.Open or onceki.High < df.High.iloc[-bak:-1].max()
    kirdi = son.High > onceki.High and son.Close > onceki.High
    return bool(geri_cekilme and kirdi), float(df.Low.iloc[-3:].min())


def seviyeler(giris, destek, a, tavanlar=None):
    """[A][P][B] Stop: destek/pullback dibinin biraz altı, ama girişe 1 ATR'den yakın olamaz
    (gürültüde stop olmamak için). TP'ler riskin 1.5 / 2.5 / 4 katı (R).
    [G] 'tavanlar' (önceki gün zirvesi, günlük 200 ort., gap/window sınırı...) içinde
    TP1'den önce gelen varsa önü kapalı sayılır."""
    stop = min(destek - STOP_ATR_TAMPON * a, giris - a)
    risk = giris - stop
    tps = [giris + k * risk for k in (MIN_RR, 2.5, 4.0)]
    engel = [t for t in (tavanlar or []) if t and giris < t < tps[0]]
    return stop, risk, tps, not engel


def pozisyon_buyuklugu(kasa, giris, stop, risk_pct=KASA_RISK_PCT):
    """[R] 1R = kasanın %1'i. Stop olursan tam 1R kaybedecek kadar adet al."""
    r_dolar = kasa * risk_pct / 100
    adet = int(r_dolar / max(giris - stop, 1e-9))
    return min(adet, int(kasa / giris)), r_dolar


def sinyal_degerlendir(df, kirilan_seviye, onceki_gun_zirvesi=None, gunluk_rvol_degeri=None,
                       diger_direncler=None, kasa=None, min_gunluk_rvol=None):
    """Botun ani hareket / zirve kırılımı sinyalini filtreler ve yeni seviyeleri hesaplar."""
    df = df.dropna()
    if len(df) < 25:
        return {'gonder': False, 'neden': 'yetersiz veri'}
    son = df.iloc[-1]
    fiyat = float(son.Close)
    a = float(atr(df).iloc[-1])
    vw = float(vwap(df).iloc[-1])
    nedenler = []

    # 0) [P][RV] Hisse seçimi: fiyat ve günlük relative volume
    if fiyat < MIN_FIYAT:
        nedenler.append(f'fiyat {MIN_FIYAT}$ altında')
    esik = MIN_GUNLUK_RVOL if min_gunluk_rvol is None else min_gunluk_rvol   # BIST için 3x verilir
    if gunluk_rvol_degeri is not None and gunluk_rvol_degeri < esik:
        nedenler.append(f'günlük RVOL düşük ({gunluk_rvol_degeri:.1f}x < {esik:.0f}x)')

    # 1) [F][V] Kırılım hacimli ve kapanış seviyenin üstünde olmalı
    rv = rvol(df)
    if rv < MIN_RVOL_KIRILIM:
        nedenler.append(f'hacimsiz kırılım (mum RVOL {rv:.1f}x)')
    if son.Close <= kirilan_seviye:
        nedenler.append('kapanış kırılan seviyenin altında (fitil)')

    # 2) [V] Fiyat VWAP üstünde olmalı, ama çok uzaklaşmışsa kovalama
    if fiyat < vw:
        nedenler.append('fiyat VWAP altında')
    elif a > 0 and (fiyat - vw) / a > MAX_VWAP_UZAKLIK_ATR:
        nedenler.append(f'VWAP’tan {(fiyat - vw) / a:.1f} ATR uzak — geri çekilme bekle')

    # 3) [D] Son mum hacimli doji ise (tepede) giriş verme
    if hacimli_doji(df):
        nedenler.append('tepede hacimli doji (dönüş riski)')

    # 4) [P] Mikro pullback varsa destek = pullback dibi (daha az risk)
    mp, mp_dip = mikro_pullback(df)
    destek = max(kirilan_seviye, vw) if fiyat > vw else kirilan_seviye
    if mp and mp_dip < fiyat:
        destek = max(destek, mp_dip)
    destek = min(destek, fiyat)

    # 5) [A][B][G] Seviyeler ve önü açık mı
    tavanlar = [onceki_gun_zirvesi] + list(diger_direncler or [])
    stop, risk, tps, onu_acik = seviyeler(fiyat, destek, a, tavanlar)
    risk_pct = risk / fiyat * 100
    if risk_pct > MAX_RISK_PCT:
        nedenler.append(f'risk çok büyük (%{risk_pct:.1f})')
    if not onu_acik:
        nedenler.append('TP1’den önce direnç var (önü kapalı)')

    guc = 'GÜÇLÜ' if not nedenler else 'ZAYIF'
    ek = (f"\nGiriş: {fiyat:.2f} (VWAP {vw:.2f})"
          f"\nKademeler: {' - '.join(f'{t:.2f}' for t in tps)}"
          f"\nStop: {stop:.2f} altı"
          f"\n📉 Risk: %{risk_pct:.1f} · 🎯 TP1 +%{(tps[0] / fiyat - 1) * 100:.1f} (R/R 1:{MIN_RR})"
          f"\nHacim: {rv:.1f}x")
    if gunluk_rvol_degeri:
        ek += f" · Günlük RVOL {gunluk_rvol_degeri:.0f}x"
    if mp:
        ek += " · Mikro pullback ✅"
    ek += f" · Sinyal: {guc}"
    ek += "\nPlan: TP1'de %33 sat + stop girişe · TP2'de %33 · kalanı iz süren stop"   # [T][K]
    if kasa:
        adet, r = pozisyon_buyuklugu(kasa, fiyat, stop)
        ek += f"\n{kasa:.0f}$ kasa, %{KASA_RISK_PCT:.0f} risk (1R={r:.0f}$): {adet} adet"
    return {'gonder': not nedenler, 'neden': '; '.join(nedenler) or 'tüm filtreler geçti',
            'stop': stop, 'hedefler': tps, 'mesaj_ek': ek}


def cikis_uyarisi(df, kirilan_seviye, giris_zamani):
    """[F][D][V][P] Açık sinyal takibi: bunlardan biri olursa 'ÇIKIŞ' uyarısı gönder."""
    sonra = df[df.index > giris_zamani]
    if sonra.empty:
        return None
    if len(sonra) <= 6 and sonra.Close.iloc[-1] < kirilan_seviye:
        return 'Fiyat kırılan seviyenin altına döndü — sahte kırılım, stopu beklemeden çık.'
    if len(sonra) >= 3 and hacim_sonuyor(sonra):
        return 'Yükseliş hacimsiz devam ediyor — pozisyonun yarısını azalt.'
    if hacimli_doji(df):
        return 'Hacimli doji oluştu — kâr al / stopu yukarı çek.'
    if sonra.Close.iloc[-1] < vwap(df).iloc[-1]:
        return 'Mum VWAP altında kapandı — çıkış sinyali.'
    son = df.iloc[-1]
    a = float(atr(df).iloc[-1])
    if son.Close < son.Open and (son.High - son.Low) > 2 * a:   # "boğalar merdivenden, ayılar pencereden"
        return 'Büyük kırmızı mum (2 ATR üstü) — kalan pozisyonu kapat.'
    return None


def iz_suren_stop(df, eski_stop, bar=3):
    """[K][P] Stop sadece yukarı taşınır: son 'bar' mumun (son birikme bölgesinin) dibine."""
    return max(eski_stop, float(df.Low.iloc[-bar:].min()))
