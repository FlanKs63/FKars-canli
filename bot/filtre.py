"""Canlı alarmı sinyal_filtreleri.py ile süzen ve açılan sinyalleri takip eden yardımcılar.

Akış (live_alerts.run_once):
  1) bot/live.detect bir "ANİ HAREKET" bulur
  2) evaluate(): o hissenin 5 dk mumları + günlük ortalama hacmiyle sinyal_degerlendir çalışır
     - gonder=False → mesaj gitmez, neden log'a "ATLANDI" diye yazılır
     - gonder=True  → mesajdaki giriş/kademe/stop satırları filtrenin seviyeleriyle değişir
  3) Gönderilen sinyal OpenSignal olarak kaydedilir; check_exit() her 5 dk'da çıkış koşullarına bakar
     (stop, sahte kırılım, hacimsiz yükseliş, hacimli doji, VWAP altı, büyük kırmızı mum)

Ayarlar: LIVE_FILTERS=0 (filtreleri kapatır, eski davranış) · LIVE_RVOL_US=5 · LIVE_RVOL_BIST=3
         LIVE_TRACK_HOURS=6 · LIVE_EXIT_CHECK_SEC=300 · LIVE_KASA (ör. 1000 → adet önerisi) · FINNHUB_KEY
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import sinyal_filtreleri as sf

from .common import env_float, env_str, redact, with_footer
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
    return res


def filtered_message(alert, res: dict, news_line: str = "") -> str:
    """Eski giriş/kademe/stop satırları yerine filtrenin seviyeleri; başlık ve imza aynı."""
    delay = " (15 dk gecikmeli veri)" if alert.delayed else ""
    lines = [
        f"⚡ {alert.display} ANİ HAREKET{delay}",
        "",
        f"Fiyat: {fmt_price(alert.price)} (son 5 dk %{alert.jump5 * 100:+.1f} · "
        f"günlük %{alert.day_change * 100:+.1f})",
        *[f"• {r}" for r in alert.reasons],
        *res["mesaj_ek"].strip("\n").split("\n"),
    ]
    if news_line:
        lines.append(news_line)
    return with_footer(lines)


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


def check_exit(sig: OpenSignal, df5: pd.DataFrame | None) -> str | None:
    """Çıkış mesajı gerekiyorsa metni döndürür; gerekmiyorsa stop'u günceller ve None döner."""
    if df5 is None or len(df5) < 5:
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
        pct = (at / sig.entry - 1) * 100
        return with_footer([f"🚪 {sig.display} ÇIKIŞ: {reason}", "",
                            f"Giriş {fmt_price(sig.entry)} → {fmt_price(at)} (%{pct:+.1f})"])

    if float(after["Low"].min()) <= sig.stop:
        label = "Stop çalıştı" if not sig.tp1_hit else "İz süren stop çalıştı (kâr korundu)"
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
