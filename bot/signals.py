"""Teknik göstergeler, puanlama, giriş/kademe/stop hesabı ve Telegram mesaj formatı.

Hem borsa verisi (hisse, BYF, GYO, altın, dolar, bitcoin) hem de yalnızca
kapanış fiyatı olan fon verisi (TEFAS) için ortak kullanılır.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .common import env_float, env_str, gemini_text, with_footer

MIN_BARS = 120            # bundan kısa geçmişi olan varlık analiz edilmez
MAX_60D_RETURN = 0.40     # son 60 günde %40+ yükselmişse "geç kalınmış" cezası

# Varlık sınıfı -> kullanıcıya gösterilecek etiket
CLASS_LABELS = {
    "hisse": "ABD hisse",
    "bist": "BIST hisse",
    "endeks": "Endeks",
    "byf": "Borsa yatırım fonu (BYF)",
    "gyo": "GYO",
    "emtia": "Emtia",
    "doviz": "Döviz",
    "kripto": "Kripto",
    "fon_hisse": "Hisse senedi fonu",
    "fon_teknoloji": "Büyüme / teknoloji fonu",
    "fon_yabanci": "Yabancı hisse fonu",
    "fon_altin": "Altın fonu",
    "fon_gumus": "Gümüş fonu",
    "fon_katilim": "Katılım fonu",
    "fon_borclanma": "Borçlanma araçları / kira sertifikası fonu",
    "fon_degisken": "Değişken fon",
    "fon_sepeti": "Fon sepeti fonu",
    "fon_doviz": "Döviz / Eurobond fonu",
}

# Hacim verisi olmayan / güvenilmez sınıflar
NO_VOLUME_CLASSES = {"endeks", "emtia", "doviz"} | {c for c in CLASS_LABELS if c.startswith("fon_")}


# =============================================================================
# GÖSTERGELER
# =============================================================================

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    out = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    # Hiç düşüş yoksa RSI 100, hiç hareket yoksa 50
    out = out.where(~(loss == 0), 100.0)
    out = out.where(~((loss == 0) & (gain == 0)), 50.0)
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(series, fast) - ema(series, slow)
    return line, ema(line, signal)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["High"].diff()
    down_move = -df["Low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = atr(df, period).replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Kolon adlarını standartlaştırır; High/Low/Volume yoksa (fonlar) üretir."""
    df = df.copy()
    df.columns = [str(c).strip().title() for c in df.columns]
    if "Close" not in df.columns:
        raise ValueError("Close kolonu yok")
    df = df[pd.to_numeric(df["Close"], errors="coerce") > 0]
    for col in ("High", "Low"):
        if col not in df.columns:
            df[col] = df["Close"]
        df[col] = df[col].fillna(df["Close"])
    if "Volume" not in df.columns:
        df["Volume"] = 0.0
    df["Volume"] = df["Volume"].fillna(0.0)
    # Bozuk satırlar (High < Close gibi) ATR'yi şişirmesin
    df["High"] = df[["High", "Close"]].max(axis=1)
    df["Low"] = df[["Low", "Close"]].min(axis=1)
    return df[["High", "Low", "Close", "Volume"]].astype(float)


# =============================================================================
# SİNYAL
# =============================================================================

@dataclass
class Signal:
    symbol: str
    display: str
    asset_class: str
    score: float
    price: float
    rsi: float
    adx: float
    relative_volume: float
    daily_change: float
    return_60d: float
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    term: str
    dip_buy: bool
    trend_long: bool
    last_date: str
    reasons: list[str] = field(default_factory=list)
    t1_days: float | None = None     # geçmişte benzer yükselişin medyan süresi (iş günü)
    t1_rate: float | None = None     # geçmişte bu yükselişin 60 iş günü içinde gelme oranı


def compute_features(df_raw: pd.DataFrame, asset_class: str) -> pd.DataFrame:
    """Her gün (satır) için göstergeleri, skoru ve seviyeleri hesaplar.

    Tüm hesaplar yalnızca o güne kadarki veriyi kullanır (geleceği görmez).
    Canlı bot son satırı, backtest ise tüm satırları kullanır; böylece ikisi
    birebir aynı kurallarla çalışır.
    """
    df = normalize_ohlcv(df_raw).dropna(subset=["Close"])
    close = df["Close"]
    n = np.arange(len(df))

    f = pd.DataFrame(index=df.index)
    f["price"] = close
    f["ema20"] = ema(close, 20)
    f["ema50"] = ema(close, 50)
    f["ema200"] = ema(close, 200).where(n >= 199)
    f["rsi"] = rsi(close)
    macd_line, macd_sig = macd(close)
    f["macd_up"] = macd_line > macd_sig
    f["adx"] = adx(df)
    f["atr"] = atr(df)

    vol = df["Volume"]
    f["has_volume"] = (asset_class not in NO_VOLUME_CLASSES) & (vol.rolling(20, min_periods=1).sum() > 0)
    avg_vol = vol.shift(1).rolling(20, min_periods=20).mean()
    f["rel_vol"] = (vol / avg_vol.replace(0, np.nan)).fillna(0.0).where(f["has_volume"], 0.0)
    f["turnover20"] = (close * vol).rolling(20, min_periods=1).mean()

    f["daily_change"] = close.pct_change().fillna(0.0)
    f["return_60d"] = (close / close.shift(60) - 1).fillna(0.0)
    f["breakout_20d"] = close > close.shift(1).rolling(20, min_periods=20).max()
    f["trend_long"] = (f["ema50"] > f["ema200"]).fillna(False) & f["ema200"].notna()

    # ---------------- puanlama ----------------
    rsi_ok = f["rsi"].between(45, 65)
    adx_ok = f["adx"] >= 20
    vol_pts = np.where(f["has_volume"], np.where(f["rel_vol"] >= 2, 15, 0),
                       np.where(f["macd_up"] & adx_ok, 15, 0))
    score = (
        10 * (f["price"] > f["ema20"]) + 15 * (f["ema20"] > f["ema50"]) + 15 * f["trend_long"]
        + 10 * rsi_ok + 10 * f["macd_up"] + 10 * adx_ok + vol_pts + 15 * f["breakout_20d"]
        - 20 * (f["return_60d"] > MAX_60D_RETURN) - 10 * (f["rsi"] > 75)
    ).astype(float)

    # ---------------- seviyeler (ATR tabanlı) ----------------
    vol_pct = (f["atr"] / f["price"]).clip(0.003, 0.15)
    low_vol = vol_pct < 0.01
    f["vol_pct"] = vol_pct
    f["entry_low"] = f["price"] * (1 - vol_pct * 0.7)
    f["entry_high"] = f["price"] * (1 + vol_pct * 0.3)
    risk_pct = (vol_pct * 1.5).clip(upper=0.12).where(lambda x: x >= np.where(low_vol, 0.015, 0.04),
                                                      np.where(low_vol, 0.015, 0.04))
    f["stop"] = f["entry_low"] * (1 - risk_pct)
    step_pct = (vol_pct * 2.0).clip(upper=0.10).where(lambda x: x >= np.where(low_vol, 0.01, 0.04),
                                                      np.where(low_vol, 0.01, 0.04))
    for i, mult in enumerate((1, 2, 3, 4, 6.5), start=1):
        f[f"t{i}"] = f["entry_high"] * (1 + step_pct * mult)

    # ---------------- "bugün düştü, al bölgesinde" ----------------
    dip_threshold = np.maximum(env_float("DIP_MIN_DROP", 0.02), vol_pct)
    tol = np.maximum(0.02, vol_pct)
    near_support = (((f["price"] - f["ema20"]).abs() / f["price"] <= tol)
                    | ((f["price"] - f["ema50"]).abs() / f["price"] <= tol))
    f["dip_buy"] = ((f["daily_change"] <= -dip_threshold) & f["trend_long"]
                    & (f["price"] >= f["ema50"] * 0.97) & f["rsi"].between(30, 55) & near_support)
    f["score"] = (score + 10 * f["dip_buy"]).clip(0, 100)

    momentum = f["breakout_20d"] | (f["has_volume"] & (f["rel_vol"] >= 2)) | (f["macd_up"] & (f["price"] > f["ema20"]))
    f["momentum"] = momentum
    f["valid"] = n >= MIN_BARS - 1
    return f


def _term(trend_long: bool, momentum: bool) -> str:
    if trend_long and momentum:
        return "Kısa vade + Uzun vade"
    if trend_long:
        return "Uzun vade"
    if momentum:
        return "Kısa vade"
    return "Kısa-orta vade (izle)"


def _reasons(r) -> list[str]:
    out = []
    if r.price > r.ema20:
        out.append("Fiyat EMA20 üzerinde")
    if r.ema20 > r.ema50:
        out.append("EMA20 > EMA50")
    if r.trend_long:
        out.append("EMA50 > EMA200 (ana trend yukarı)")
    if 45 <= r.rsi <= 65:
        out.append(f"RSI sağlıklı ({r.rsi:.0f})")
    if r.macd_up:
        out.append("MACD yukarı kesişimde")
    if r.adx >= 20:
        out.append(f"Trend gücü yeterli (ADX {r.adx:.0f})")
    if r.has_volume and r.rel_vol >= 2:
        out.append(f"Hacim ortalamanın {r.rel_vol:.1f} katı")
    elif not r.has_volume and r.macd_up and r.adx >= 20:
        out.append("Trend ve momentum uyumlu")
    if r.breakout_20d:
        out.append("20 günlük zirveyi kırdı")
    if r.return_60d > MAX_60D_RETURN:
        out.append(f"Son 60 günde zaten %{r.return_60d * 100:.0f} yükselmiş")
    if r.rsi > 75:
        out.append(f"Aşırı alım bölgesi (RSI {r.rsi:.0f})")
    if r.dip_buy:
        out.append(f"Bugün %{abs(r.daily_change) * 100:.1f} geri çekildi, destek bölgesinde")
    return out


def signal_from_row(r, symbol: str, display: str, asset_class: str) -> Signal:
    return Signal(
        symbol=symbol, display=display, asset_class=asset_class, score=float(r.score),
        price=float(r.price), rsi=float(r.rsi), adx=float(r.adx), relative_volume=float(r.rel_vol),
        daily_change=float(r.daily_change), return_60d=float(r.return_60d),
        entry_low=float(r.entry_low), entry_high=float(r.entry_high), stop=float(r.stop),
        targets=[float(r.t1), float(r.t2), float(r.t3), float(r.t4), float(r.t5)],
        term=_term(bool(r.trend_long), bool(r.momentum)), dip_buy=bool(r.dip_buy),
        trend_long=bool(r.trend_long), last_date=str(pd.Timestamp(r.Index).date()),
        reasons=_reasons(r),
    )


def target_timing(df: pd.DataFrame, pct: float, lookback: int = 250, horizon: int = 60):
    """Bu varlıkta geçmiş 1 yılda, herhangi bir günden itibaren fiyatın %pct yükselmesi
    kaç iş günü sürmüş? Dönüş: (medyan gün veya None, 60 gün içinde gelme oranı veya None).

    Tahmin değil, geçmişin özeti: "benzer bir yükseliş geçmişte genelde ne kadar sürdü".
    """
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float) if "High" in df else close
    n = len(close)
    starts = range(max(0, n - horizon - lookback), n - horizon)
    days, total = [], 0
    for i in starts:
        total += 1
        window = high[i + 1:i + 1 + horizon]
        hit = np.nonzero(window >= close[i] * (1 + pct))[0]
        if hit.size:
            days.append(int(hit[0]) + 1)
    if total < 40:
        return None, None
    rate = len(days) / total
    return (float(np.median(days)) if days else None), rate


def analyze(df_raw: pd.DataFrame, symbol: str, display: str, asset_class: str) -> Signal | None:
    """Tek bir varlığın günlük verisinden (son güne göre) sinyal üretir. Veri yetersizse None."""
    try:
        f = compute_features(df_raw, asset_class)
    except (ValueError, KeyError):
        return None
    if len(f) < MIN_BARS:
        return None
    row = next(f.iloc[[-1]].itertuples())
    sig = signal_from_row(row, symbol, display, asset_class)
    try:
        sig.t1_days, sig.t1_rate = target_timing(normalize_ohlcv(df_raw), sig.targets[0] / sig.price - 1)
    except (ValueError, KeyError):
        pass
    return sig


def is_candidate(sig: Signal, min_score: float, min_dip_score: float) -> bool:
    return sig.score >= min_score or (sig.dip_buy and sig.score >= min_dip_score)


def should_send(state, sig: Signal, cooldown_days: int) -> bool:
    """Aynı varlık için gereksiz tekrar mesajı engeller.

    - Aynı günlük mum için asla iki kez gönderilmez (tatil günü vb.).
    - Bekleme süresi içinde yalnızca yeni bir "düşüşte al" fırsatı gönderilir.
    """
    prev = state.get(sig.symbol)
    if prev is None:
        return True
    if prev.get("last_date") == sig.last_date:
        return False
    days = state.days_since(sig.symbol)
    if days is not None and days < cooldown_days:
        return sig.dip_buy and not prev.get("dip", False)
    return True


# =============================================================================
# MESAJ
# =============================================================================

def fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:.0f}"
    if x >= 1:
        return f"{x:.2f}"
    if x >= 0.01:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.6f}".rstrip("0").rstrip(".")


def news_note(sig: Signal) -> str:
    """Gemini + Google Search ile son haberlerin 1-2 cümlelik özeti (anahtar yoksa boş)."""
    label = CLASS_LABELS.get(sig.asset_class, sig.asset_class)
    name = sig.display.lstrip("$")
    prompt = (
        f"{name} ({label}, Yahoo sembolü {sig.symbol}) hakkında son 7 günün önemli haberlerini "
        "internette ara ve Türkçe en fazla 2 kısa cümleyle özetle (bilanço, anlaşma, dava, "
        "yönetim, sektör, makro). Önemli haber yoksa sadece 'Önemli haber yok.' yaz. "
        "'Al' veya 'sat' deme, fiyat tahmini yapma, kaynağı olmayan rakam yazma."
    )
    return gemini_text(prompt, max_chars=260, search=True)


def technical_line(sig: Signal) -> str:
    keep = [r for r in sig.reasons if "yükselmiş" not in r and "Aşırı alım" not in r]
    return "📊 Teknik: " + (", ".join(keep[:4]) if keep else "zayıf")


TRADE_TYPE = {
    "Kısa vade + Uzun vade": "Swing + Position trade (günler → aylar)",
    "Uzun vade": "Position trade (haftalar → aylar)",
    "Kısa vade": "Swing trade (günler → haftalar)",
    "Kısa-orta vade (izle)": "Swing trade (izle)",
}


def risk_reward(sig: Signal, level: int) -> float | None:
    risk = sig.price - sig.stop
    if risk <= 0:
        return None
    return (sig.targets[level - 1] - sig.price) / risk


def action_label(sig: Signal, weak_market: bool = False) -> str:
    """İzle / Araştır / Uzak dur. Gönderilen sinyaller zaten eşiği geçmiştir;
    burada güç derecesi ayrılır."""
    if weak_market:
        return "Araştır 🔎 (piyasa zayıf)"
    if sig.t1_rate is not None and sig.t1_rate < 0.4:
        return "Araştır 🔎 (hedefe ulaşma geçmişi zayıf)"
    if sig.score >= 80 or (sig.dip_buy and sig.trend_long):
        return "İzle ✅"
    return "Araştır 🔎"


def timing_line(sig: Signal) -> str:
    if sig.t1_days is None or sig.t1_rate is None:
        return "⏳ Tahmini süre: yeterli geçmiş yok"
    if sig.t1_rate == 0:
        return "⏳ Tahmini süre: geçmiş 1 yılda bu kadarlık yükseliş 60 iş gününde hiç gelmedi"
    return (f"⏳ Tahmini süre: 1. kademe ~{sig.t1_days:.0f} iş günü "
            f"(geçmiş 1 yılda %{sig.t1_rate * 100:.0f} ihtimalle geldi)")


def build_message(sig: Signal, extra_line: str = "", with_ai: bool = True,
                  weak_market: bool = False) -> str:
    """Mesaj formatı (FIRST_PERSON=0 ile "giriş uygun / Kademeler / Stop" olur):

    $MTEN

    1.15-1.20 giriş sağlayacağım

    Kademelerim:1.25-1.32-1.38-1.44-1.50++

    Stopum:1.05 altı
    """
    first = env_str("FIRST_PERSON", "1") != "0"
    entry_word, kad_word, stop_word = (("giriş sağlayacağım", "Kademelerim", "Stopum") if first
                                       else ("giriş uygun", "Kademeler", "Stop"))
    lines: list[str] = []
    if sig.dip_buy:
        lines += ["🔻 BUGÜN DÜŞTÜ — AL BÖLGESİNDE", ""]

    kademeler = "-".join(fmt_price(t) for t in sig.targets[:-1]) + f"-{fmt_price(sig.targets[-1])}++"
    risk = (sig.price - sig.stop) / sig.price * 100
    gain1 = (sig.targets[0] / sig.price - 1) * 100
    gain3 = (sig.targets[2] / sig.price - 1) * 100
    rr1, rr3 = risk_reward(sig, 1), risk_reward(sig, 3)
    rr_txt = (f"⚖️ Risk/Ödül: 1:{rr1:.1f} (TP1) · 1:{rr3:.1f} (TP3)"
              if rr1 is not None and rr3 is not None else "⚖️ Risk/Ödül: hesaplanamadı")
    lines += [
        sig.display,
        "",
        f"{fmt_price(sig.entry_low)}-{fmt_price(sig.entry_high)} {entry_word}",
        "",
        f"{kad_word}:{kademeler}",
        "",
        f"{stop_word}:{fmt_price(sig.stop)} altı",
        "",
        f"📍 Al noktası: {fmt_price(sig.entry_low)} (ideal) · en fazla {fmt_price(sig.entry_high)}",
        f"🚪 Çıkış: kademelerde parça parça sat · {fmt_price(sig.stop)} altında kapanışta çık",
        f"📉 Risk: %{risk:.1f} · 🎯 TP1 +%{gain1:.1f} · TP3 +%{gain3:.1f}",
        rr_txt,
        timing_line(sig),
        f"📌 {TRADE_TYPE.get(sig.term, sig.term)}",
    ]
    note = news_note(sig) if with_ai else ""
    if note:
        lines.append(f"📰 Haber: {note}")
    lines += [
        technical_line(sig),
        f"Tür: {CLASS_LABELS.get(sig.asset_class, sig.asset_class)} · Skor {sig.score:.0f}/100",
    ]
    if extra_line:
        lines.append(extra_line)
    lines += ["", f"Aksiyon: {action_label(sig, weak_market)}"]
    return with_footer(lines)
