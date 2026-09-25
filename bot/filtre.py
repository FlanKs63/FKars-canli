"""Canlı alarmı sinyal_filtreleri.py ile süzen ve açılan sinyalleri takip eden yardımcılar.

Akış (live_alerts.run_once):
  1) bot/live.detect bir "ANİ HAREKET" bulur
  2) evaluate(): o hissenin 5 dk mumları + günlük ortalama hacmiyle sinyal_degerlendir çalışır
     - gonder=False → mesaj gitmez, neden log'a "ATLANDI" diye yazılır
     - gonder=True  → mesajdaki giriş/kademe/stop satırları filtrenin seviyeleriyle değişir
  3) Gönderilen sinyal OpenSignal olarak kaydedilir; check_exit() her 5 dk'da çıkış koşullarına bakar
     (stop, sahte kırılım, hacimsiz yükseliş, hacimli doji, VWAP altı, büyük kırmızı mum)

Ayarlar: LIVE_FILTERS=0 (filtreleri kapatır, eski davranış) · LIVE_MAX_SOFT=1 · LIVE_RVOL_US=5 · LIVE_RVOL_BIST=3
         LIVE_TRACK_HOURS=6 · LIVE_EXIT_CHECK_SEC=300 · LIVE_KASA (ör. 1000 → adet önerisi) · FINNHUB_KEY
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import sinyal_filtreleri as sf

from .common import env_float, env_str, redact, with_footer
from .live import buy_message
from .market_data import _extract
from .signals import fmt_price

NY = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")

# piyasa: (saat dilimi, seans açılışı, seans uzunluğu dk)
SESSIONS = {"us": (NY, dtime(9, 30), 390), "bist": (TR, dtime(10, 0), 480)}


def enabled() -> bool:
    return env_str("LIVE_FILTERS", "1") != "0"


def min_rvol(market: str) -> float | None:
    if market == "us":
        return env_float("LIVE_RVOL_US", sf.MIN_GUNLUK_RVOL)
    if market == "bist":
        return env_float("LIVE_RVOL_BIST", 3.0)
    return None                       # kripto 7/24: seansa göre günlük RVOL anlamlı değil


# -----------------------------------------------------------------------------
# VERİ
# -----------------------------------------------------------------------------

def fetch_5m(symbols: list[str], market: str) -> dict[str, pd.DataFrame]:
    """Son 2 günün 5 dk mumları (ABD'de ön/son piyasa dahil)."""
    import yfinance as yf
    if not symbols:
        return {}
    try:
        data = yf.download(symbols, period="2d", interval="5m", prepost=(market == "us"),
                           group_by="ticker", auto_adjust=False, threads=True, progress=False)
    except Exception as e:  # noqa: BLE001
        print(redact(f"  5 dk veri alınamadı: {e}"))
        return {}
    out = {}
    for sym in symbols:
        df = _extract(data, sym)
        if df is not None and len(df):
            out[sym] = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    return out


_daily_cache: dict[tuple[str, str], float | None] = {}


def avg_daily_volume(symbol: str, today: str) -> float | None:
    """Son 30 tamamlanmış günün ortalama hacmi (gün içinde bir kez indirilir)."""
    key = (symbol, today)
    if key in _daily_cache:
        return _daily_cache[key]
    val = None
    try:
        import yfinance as yf
        df = _extract(yf.download(symbol, period="3mo", interval="1d", auto_adjust=False,
                                  progress=False, threads=False), symbol)
        if df is not None and len(df):
            idx = pd.DatetimeIndex(df.index)
            past = df[[str(d.date()) < today for d in idx]]
            vol = past["Volume"].tail(30)
            val = float(vol.mean()) if len(vol) and vol.mean() > 0 else None
    except Exception as e:  # noqa: BLE001
        print(redact(f"  {symbol} günlük hacim alınamadı: {e}"))
    _daily_cache[key] = val
    return val


def _local_dates(df: pd.DataFrame, tz: ZoneInfo) -> list:
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return [d.date() for d in idx.tz_convert(tz)]


def daily_rvol(df5: pd.DataFrame, market: str, now: datetime, avg_vol: float | None) -> float | None:
    """Bugünkü hacim, seansın geçen kısmına göre beklenen hacimle kıyaslanır (sinyal_filtreleri.gunluk_rvol)."""
    if market not in SESSIONS or not avg_vol:
        return None
    tz, open_t, session_min = SESSIONS[market]
    local = now.astimezone(tz)
    dates = _local_dates(df5, tz)
    today_vol = float(df5["Volume"][[d == local.date() for d in dates]].sum())
    opened = datetime.combine(local.date(), open_t, tz)
    minutes = (local - opened).total_seconds() / 60
    return sf.gunluk_rvol(today_vol, avg_vol, minutes, seans_dk=session_min)


def previous_day_high(df5: pd.DataFrame, market: str) -> float | None:
    tz = SESSIONS.get(market, (ZoneInfo("UTC"),))[0]
    dates = _local_dates(df5, tz)
    days = sorted(set(dates))
    if len(days) < 2:
        return None
    prev = df5[[d == days[-2] for d in dates]]
    return float(prev["High"].max()) if len(prev) else None


# -----------------------------------------------------------------------------
# DEĞERLENDİRME
# -----------------------------------------------------------------------------

def evaluate(alert, df5: pd.DataFrame | None, now: datetime, avg_vol: float | None = None) -> dict:
    """alert: bot.live.Alert. Dönüş sinyal_filtreleri.sinyal_degerlendir sonucu + 'kirilan'."""
    if df5 is None or len(df5) < 25:
        return {"gonder": False, "neden": "5 dk veri yok/yetersiz"}
    kirilan = alert.prior_high if alert.breakout else float(df5["High"].iloc[-2])
    kasa = env_float("LIVE_KASA", 0) or None
    res = sf.sinyal_degerlendir(
        df5, kirilan_seviye=kirilan, onceki_gun_zirvesi=previous_day_high(df5, alert.market),
        gunluk_rvol_degeri=daily_rvol(df5, alert.market, now, avg_vol), kasa=kasa,
        min_gunluk_rvol=min_rvol(alert.market))
    res["kirilan"] = kirilan
    return apply_policy(res)


# Esnek kurallar: biri tek başına bozulursa sinyal "ORTA ⚠️" etiketiyle yine gönderilir.
# Geri kalan her şey (kırılım fitilde kaldı, fiyat VWAP altında, tepede doji, risk > %6, çok ucuz hisse)
# zorunludur: biri bile bozulursa gönderilmez.
SOFT_RULES = ("günlük RVOL düşük", "hacimsiz kırılım", "VWAP’tan", "TP1’den önce direnç")


def apply_policy(res: dict) -> dict:
    """sinyal_degerlendir sonucunu zorunlu/esnek kurala göre yeniden karar verir.
    LIVE_MAX_SOFT (varsayılan 1): kaç esnek kural bozukken gönderilsin. 0 = arkadaşın katı modu."""
    reasons = [r for r in (res.get("neden") or "").split("; ") if r and r != "tüm filtreler geçti"]
    soft = [r for r in reasons if r.startswith(SOFT_RULES)]
    hard = [r for r in reasons if r not in soft]
    max_soft = int(env_float("LIVE_MAX_SOFT", 1))
    out = dict(res)
    out["zorunlu"], out["esnek"] = hard, soft
    if "mesaj_ek" not in res:                         # yetersiz veri gibi erken çıkışlar
        out["gonder"] = False
        return out
    out["gonder"] = not hard and len(soft) <= max_soft
    if out["gonder"]:
        label = "GÜÇLÜ ✅" if not soft else f"ORTA ⚠️ ({'; '.join(soft)})"
        out["mesaj_ek"] = re.sub(r"Sinyal: (ZAYIF|GÜÇLÜ)", f"Sinyal: {label}", res["mesaj_ek"], count=1)
    return out


# Esnek kural metinlerinin mesajdaki kısa, anlaşılır karşılıkları
SOFT_SHORT = (("günlük RVOL düşük", "günlük hacim zayıf"),
              ("hacimsiz kırılım", "kırılım mumu hacimsiz"),
              ("VWAP’tan", "fiyat çok koşmuş, geri çekilme bekle"),
              ("TP1’den önce direnç", "TP1 öncesi direnç var"))

# Giriş aralığı: fiyatın riskin 1/4'ü altı (kırılan seviyenin altına inmeden) ile riskin 0,15'i üstü.
# Üst sınırın üstünde kovalanırsa TP1'in risk/ödülü 1:1'in altına düşer.
ENTRY_BELOW_R, ENTRY_ABOVE_R = 0.25, 0.15


def entry_zone(price: float, stop: float, kirilan: float | None = None) -> tuple[float, float]:
    risk = max(price - stop, 0.0)
    low = price - ENTRY_BELOW_R * risk
    if kirilan and kirilan < price:
        low = max(low, kirilan)
    return low, price + ENTRY_ABOVE_R * risk


def filtered_message(alert, res: dict, news_line: str = "") -> str:
    """Filtreden geçen sinyal için kısa AL mesajı (seviyeler filtreden)."""
    stop, tps = float(res["stop"]), [float(t) for t in res["hedefler"]]
    low, high = entry_zone(alert.price, stop, res.get("kirilan"))
    soft = res.get("esnek") or []
    notes = []
    if soft:
        short = [next((s for k, s in SOFT_SHORT if r.startswith(k)), r) for r in soft]
        notes.append("⚠️ Dikkat: " + ", ".join(short))
    if news_line:
        notes.append(news_line if len(news_line) <= 160 else news_line[:157].rstrip() + "…")
    kasa = env_float("LIVE_KASA", 0)
    if kasa and stop < alert.price:
        adet, r = sf.pozisyon_buyuklugu(kasa, alert.price, stop)
        notes.append(f"💰 {kasa:.0f}$ kasa: {adet} adet (stop olursa -{r:.0f}$)")
    notes.append("💡 TP1'de 1/3 sat, stopu girişe çek")
    return buy_message(alert, low, high, stop, tps, "ORTA" if soft else "GÜÇLÜ", notes)


# -----------------------------------------------------------------------------
# FINNHUB HABER (yalnız ABD hisseleri)
# -----------------------------------------------------------------------------

def finnhub_news(symbol: str, now: datetime, session=None) -> str | None:
    """Son 24 saatteki ilk haber başlığı. Anahtar yoksa ya da hata olursa None (Gemini'ye düşülür)."""
    key = env_str("FINNHUB_KEY")
    if not key:
        return None
    import requests
    session = session or requests
    day = now.astimezone(NY).date()
    try:
        r = session.get("https://finnhub.io/api/v1/company-news", timeout=10, params={
            "symbol": symbol, "from": str(day - timedelta(days=1)), "to": str(day), "token": key})
        if r.status_code != 200:
            print(f"  Finnhub HTTP {r.status_code} ({symbol})")
            return None
        items = r.json() or []
    except Exception as e:  # noqa: BLE001
        print(redact(f"  Finnhub hatası ({symbol}): {e}"))
        return None
    cutoff = now.timestamp() - 24 * 3600
    recent = [it for it in items if isinstance(it, dict) and float(it.get("datetime") or 0) >= cutoff]
    recent.sort(key=lambda it: float(it.get("datetime") or 0), reverse=True)
    if recent and recent[0].get("headline"):
        return f"📰 Haber: {str(recent[0]['headline']).strip()[:200]}"
    return "📰 Haber yok (dikkat)"


# -----------------------------------------------------------------------------
# AÇIK SİNYAL TAKİBİ
# -----------------------------------------------------------------------------

@dataclass
class OpenSignal:
    symbol: str
    display: str
    market: str
    entry: float
    stop: float
    tp1: float
    kirilan: float
    opened: str            # ISO zaman (UTC)
    tp1_hit: bool = False
    exit_price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OpenSignal":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def open_from(alert, res: dict, now: datetime) -> OpenSignal:
    return OpenSignal(symbol=alert.symbol, display=alert.display, market=alert.market,
                      entry=alert.price, stop=float(res["stop"]), tp1=float(res["hedefler"][0]),
                      kirilan=float(res["kirilan"]), opened=now.isoformat())


def expired(sig: OpenSignal, now: datetime) -> bool:
    hours = env_float("LIVE_TRACK_HOURS", 6)
    return now - datetime.fromisoformat(sig.opened) > timedelta(hours=hours)


def closed_bars(df5: pd.DataFrame, now: datetime | None, minutes: int = 5) -> pd.DataFrame:
    """Henüz kapanmamış son mumu at (yfinance oluşmakta olan mumu da verir): yarım mumun hacmi
    'hacim sönüyor' / VWAP altı gibi çıkış kurallarını haksız yere tetiklemesin."""
    if now is None or not len(df5):
        return df5
    last = pd.Timestamp(df5.index[-1])
    if last.tzinfo is None:
        last = last.tz_localize("UTC")
    return df5.iloc[:-1] if last + pd.Timedelta(minutes=minutes) > pd.Timestamp(now) else df5


# sinyal_filtreleri.cikis_uyarisi metni → (simge, ne yapılsın, kısa neden)
EXIT_SHORT = (("Fiyat kırılan seviyenin altına döndü", "🔴", "SAT", "sahte kırılım"),
              ("Yükseliş hacimsiz", "🟠", "YARISINI SAT", "hacim sönüyor"),
              ("Hacimli doji", "🟠", "KÂR AL", "tepede hacimli doji"),
              ("Mum VWAP altında", "🔴", "SAT", "VWAP altına indi"),
              ("Büyük kırmızı mum", "🔴", "SAT", "büyük kırmızı mum"))


def check_exit(sig: OpenSignal, df5: pd.DataFrame | None, now: datetime | None = None) -> str | None:
    """Çıkış mesajı gerekiyorsa metni döndürür; gerekmiyorsa stop'u günceller ve None döner."""
    if df5 is None:
        return None
    df5 = closed_bars(df5, now)
    if len(df5) < 5:
        return None
    opened = pd.Timestamp(sig.opened)
    idx = pd.DatetimeIndex(df5.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    after = df5[idx > opened]
    if after.empty:
        return None
    price = float(after["Close"].iloc[-1])

    def exit_text(reason: str, at: float) -> str:
        sig.exit_price = at
        pct = (at / sig.entry - 1) * 100
        icon, action, why = next(((i, a, w) for k, i, a, w in EXIT_SHORT if reason.startswith(k)),
                                 ("🔴", "SAT", reason))
        return with_footer([f"{icon} {sig.display} {action} — {why}",
                            f"Giriş {fmt_price(sig.entry)} → {fmt_price(at)} (%{pct:+.1f})"])

    if float(after["Low"].min()) <= sig.stop:
        label = "stop çalıştı" if not sig.tp1_hit else "iz süren stop çalıştı (kâr korundu)"
        return exit_text(label, sig.stop)
    if not sig.tp1_hit and float(after["High"].max()) >= sig.tp1:
        sig.tp1_hit = True
        sig.stop = max(sig.stop, sig.entry)            # plan: TP1'de stop girişe
    warn = sf.cikis_uyarisi(df5, sig.kirilan, opened)
    if warn:
        return exit_text(warn, price)
    if sig.tp1_hit:
        sig.stop = sf.iz_suren_stop(after, sig.stop)   # yalnız yukarı taşınır
    return None


# -----------------------------------------------------------------------------
# CANLI SONUÇ RAPORU
# -----------------------------------------------------------------------------

def record_result(results: list, sig: OpenSignal, now: datetime, price: float, reason: str) -> None:
    """Kapanan canlı sinyalin sonucunu kaydeder (state/live.json → extra.results, 30 gün tutulur)."""
    pct = (price / sig.entry - 1) if sig.entry else 0.0
    results.append({"date": str(now.astimezone(TR).date()), "sym": sig.display, "market": sig.market,
                    "pct": round(pct, 4), "reason": reason, "tp1": sig.tp1_hit})
    cutoff = str((now - timedelta(days=30)).astimezone(TR).date())
    results[:] = [r for r in results if r.get("date", "") >= cutoff]


def results_summary(results: list, since: str, title: str) -> str | None:
    rows = [r for r in results if r.get("date", "") >= since]
    if not rows:
        return None
    pcts = [r["pct"] for r in rows]
    wins = sum(1 for p in pcts if p > 0)
    tp1 = sum(1 for r in rows if r.get("tp1"))
    best = max(rows, key=lambda r: r["pct"])
    worst = min(rows, key=lambda r: r["pct"])
    lines = [title, "",
             f"Kapanan sinyal: {len(rows)} · kârda kapanan: {wins} (%{wins / len(rows) * 100:.0f})",
             f"TP1 görülen: {tp1} (%{tp1 / len(rows) * 100:.0f})",
             f"Ortalama: %{sum(pcts) / len(pcts) * 100:+.1f}",
             f"En iyi: {best['sym']} %{best['pct'] * 100:+.1f} · En kötü: {worst['sym']} %{worst['pct'] * 100:+.1f}"]
    by_m: dict[str, list] = {}
    for r in rows:
        by_m.setdefault(r.get("market", "?"), []).append(r["pct"])
    if len(by_m) > 1:
        lines.append(" · ".join(f"{m}: {len(v)} sinyal, ort %{sum(v) / len(v) * 100:+.1f}" for m, v in sorted(by_m.items())))
    return with_footer(lines)

