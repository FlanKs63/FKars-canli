"""Canlı alarm kuralları: 1 dakikalık mumlarda ani hacim patlaması, sıçrama, zirve kırılımı.

Saf fonksiyonlar (internet gerektirmez), live_alerts.py döngüsü bunları kullanır.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from . import tatil
from .common import env_float, with_footer
from .signals import fmt_price

NY = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")


@dataclass
class Alert:
    symbol: str
    display: str
    market: str
    price: float
    jump5: float          # son 5 dakikadaki değişim
    day_change: float     # günün ilk mumuna göre değişim
    vol_mult: float       # son 5 dk hacmi / önceki saatin 5 dk ortalaması
    breakout: bool
    prior_high: float
    reasons: list[str]
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    delayed: bool = False
    bar_time: str = ""


def market_open(market: str, now_utc: datetime) -> bool:
    if market == "kripto":
        return True
    if market == "us":
        ny = now_utc.astimezone(NY)
        if not tatil.is_trading_day("us", ny.date()):
            return False
        # ön piyasa 04:00 → kapanış sonrası 20:00 (ET); yarım günde son piyasa 17:00'de biter
        end = dtime(17, 0) if tatil.early_close("us", ny.date()) else dtime(20, 0)
        return dtime(4, 0) <= ny.time() < end
    if market == "bist":
        tr = now_utc.astimezone(TR)
        if not tatil.is_trading_day("bist", tr.date()):
            return False
        # Yahoo BIST verisi ~15 dk gecikmeli: açılıştan 15 dk sonra başla, kapanıştan 20 dk sonra bitir
        half = tatil.early_close("bist", tr.date())
        end = dtime(12, 50) if half else dtime(18, 30)
        return dtime(10, 15) <= tr.time() < end
    return False


# Son 1 dk mum bundan eskiyse veri bayat sayılır (dünün seansı, tatil, Yahoo gecikmesi) → alarm yok
MAX_BAR_AGE_MIN = {"us": 5, "kripto": 5, "bist": 25}


def is_fresh(df: pd.DataFrame | None, market: str, now_utc: datetime) -> bool:
    if df is None or not len(df):
        return False
    last = pd.Timestamp(df.index[-1])
    if last.tzinfo is None:
        last = last.tz_localize("UTC")
    age = (pd.Timestamp(now_utc) - last).total_seconds() / 60
    return age <= MAX_BAR_AGE_MIN.get(market, 5)


def session_part(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """Günlük değişim için bugünün mumları (kripto: son 24 saat)."""
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    if market == "kripto":
        return df[idx >= idx[-1] - pd.Timedelta(hours=24)]
    tz = NY if market == "us" else TR
    local = idx.tz_convert(tz)
    return df[[d.date() == local[-1].date() for d in local]]


def detect(df: pd.DataFrame, symbol: str, display: str, market: str,
           min_dollar_vol: float) -> Alert | None:
    """Son 1 dakikalık mumlara bakar; koşul oluşmadıysa None döner."""
    if df is None or len(df) < 30:
        return None
    df = df.dropna(subset=["Close"])
    if len(df) < 30:
        return None
    close = df["Close"].astype(float)
    vol = df["Volume"].fillna(0).astype(float)
    price = float(close.iloc[-1])
    if price <= 0:
        return None

    last5 = df.tail(5)
    prev = df.iloc[-65:-5]
    avg5 = float(vol.iloc[-65:-5].mean()) * 5 if len(prev) else 0.0
    vol5 = float(vol.tail(5).sum())
    vol_mult = vol5 / avg5 if avg5 > 0 else 0.0
    dollar5 = float((last5["Close"] * last5["Volume"].fillna(0)).sum())
    jump5 = price / float(close.iloc[-6]) - 1
    today = session_part(df, market)
    day_change = price / float(today["Open"].iloc[0] if "Open" in today else today["Close"].iloc[0]) - 1
    prior = today.iloc[:-5] if len(today) > 5 else df.iloc[:-5]
    prior_high = float(prior["High"].max())
    breakout = price > prior_high

    jump_pct = env_float("LIVE_JUMP_PCT", 0.02 if market == "kripto" else 0.03)
    spike_mult = env_float("LIVE_VOLUME_MULT", 5.0)
    has_volume = vol.tail(65).sum() > 0

    reasons = []
    if has_volume and dollar5 < min_dollar_vol:
        return None
    if has_volume and vol_mult >= spike_mult and jump5 >= 0.01:
        reasons.append(f"Hacim patlaması: son 5 dk ortalamanın {vol_mult:.1f} katı")
    if jump5 >= jump_pct and (vol_mult >= 2 or not has_volume):
        reasons.append(f"Ani sıçrama: son 5 dk %{jump5 * 100:+.1f}")
    if breakout and day_change >= 0.02 and (vol_mult >= 2 or not has_volume):
        reasons.append(f"Gün içi zirve kırıldı: {fmt_price(prior_high)}")
    if not reasons:
        return None

    rng = float(last5["High"].max() - last5["Low"].min()) / price
    step = max(0.02 if market != "kripto" else 0.012, rng)
    low15 = float(df["Low"].tail(15).min())
    stop = min(low15, price * (1 - 1.5 * step))
    return Alert(
        symbol=symbol, display=display, market=market, price=price, jump5=jump5,
        day_change=day_change, vol_mult=vol_mult, breakout=breakout, prior_high=prior_high,
        reasons=reasons, entry_low=price * (1 - step * 0.3), entry_high=price * (1 + step * 0.2),
        stop=stop, targets=[price * (1 + step * k) for k in (1, 2, 3)],
        delayed=(market == "bist"), bar_time=str(df.index[-1]),
    )


def money(x: float, market: str) -> str:
    return f"{fmt_price(x)}{'₺' if market == 'bist' else '$'}"


def why_line(a: Alert) -> str:
    """Alarmın nedenleri tek satırda: '⚡ Zirve kırıldı · hacim 10.7x · 5 dk %+10.0 · gün %+19.8'."""
    parts = []
    if any(r.startswith("Gün içi zirve") for r in a.reasons):
        parts.append("Zirve kırıldı")
    if a.vol_mult >= 2:
        parts.append(f"hacim {a.vol_mult:.1f}x")
    parts += [f"5 dk %{a.jump5 * 100:+.1f}", f"gün %{a.day_change * 100:+.1f}"]
    return "⚡ " + " · ".join(parts)


def buy_message(a: Alert, entry_low: float, entry_high: float, stop: float, targets: list[float],
                strength: str = "", notes: list[str] | tuple = ()) -> str:
    """Kısa AL mesajı: başlık, giriş aralığı, stop, kademeler; altında tek satır neden + notlar.
    strength: '' (filtresiz), 'GÜÇLÜ' ya da 'ORTA'."""
    icon = "🟡" if strength == "ORTA" else "🟢"
    tag = f" · {strength}" if strength else ""
    delay = " (15 dk gecikmeli)" if a.delayed else ""
    lo, hi = money(entry_low, a.market), money(entry_high, a.market)
    entry = lo if lo == hi else f"{fmt_price(entry_low)} – {hi}"
    risk = f" (%-{(a.price - stop) / a.price * 100:.1f})" if 0 < stop < a.price else ""
    lines = [
        f"{icon} {a.display} AL{tag}{delay}",
        "",
        f"🔹 Giriş: {entry}",
        f"🛑 Stop: {money(stop, a.market)}{risk}",
        f"🎯 Kademeler: {' → '.join(money(t, a.market) for t in targets)}",
        "",
        why_line(a),
        *[n for n in notes if n],
    ]
    return with_footer(lines)


def alert_message(a: Alert, news: str = "") -> str:
    """Filtreler kapalıyken (LIVE_FILTERS=0) giden mesaj."""
    return buy_message(a, a.entry_low, a.entry_high, a.stop, a.targets,
                       notes=[f"📰 Haber: {news}" if news else ""])


class Cooldown:
    """Aynı sembol için tekrar mesajı engeller; fiyat son alarmdan belirgin yükselirse izin verir."""

    def __init__(self, minutes: float, rearm_pct: float, max_per_hour: int):
        self.minutes = minutes
        self.rearm_pct = rearm_pct
        self.max_per_hour = max_per_hour
        self.last: dict[str, tuple[datetime, float]] = {}
        self.sent_times: list[datetime] = []

    def allow(self, symbol: str, price: float, now: datetime) -> bool:
        self.sent_times = [t for t in self.sent_times if (now - t).total_seconds() < 3600]
        if len(self.sent_times) >= self.max_per_hour:
            return False
        prev = self.last.get(symbol)
        if prev is None:
            return True
        when, prev_price = prev
        if (now - when).total_seconds() >= self.minutes * 60:
            return True
        return price >= prev_price * (1 + self.rearm_pct)

    def mark(self, symbol: str, price: float, now: datetime) -> None:
        self.last[symbol] = (now, price)
        self.sent_times.append(now)

    def to_dict(self) -> dict:
        return {s: {"time": t.isoformat(), "price": p} for s, (t, p) in self.last.items()}

    def load(self, data: dict) -> None:
        for s, v in (data or {}).items():
            try:
                self.last[s] = (datetime.fromisoformat(v["time"]), float(v["price"]))
            except (KeyError, ValueError, TypeError):
                continue
