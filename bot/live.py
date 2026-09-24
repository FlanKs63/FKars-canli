"""Canlı alarm kuralları: 1 dakikalık mumlarda ani hacim patlaması, sıçrama, zirve kırılımı.

Saf fonksiyonlar (internet gerektirmez), live_alerts.py döngüsü bunları kullanır.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

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
        # ön piyasa 04:00 → kapanış sonrası 20:00 (ET): 1 dolarlık hisselerde hareket erken başlar
        return ny.weekday() < 5 and dtime(4, 0) <= ny.time() < dtime(20, 0)
    if market == "bist":
        tr = now_utc.astimezone(TR)
        # Yahoo BIST verisi ~15 dk gecikmeli: kapanıştan sonra 20 dk daha bakılır
        return tr.weekday() < 5 and dtime(10, 0) <= tr.time() < dtime(18, 30)
    return False


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
    day_change = price / float(df["Open"].iloc[0] if "Open" in df else close.iloc[0]) - 1
    prior_high = float(df["High"].iloc[:-5].max())
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


def alert_message(a: Alert, news: str = "") -> str:
    delay = " (15 dk gecikmeli veri)" if a.delayed else ""
    lines = [
        f"⚡ {a.display} ANİ HAREKET{delay}",
        "",
        f"Fiyat: {fmt_price(a.price)} (son 5 dk %{a.jump5 * 100:+.1f} · günlük %{a.day_change * 100:+.1f})",
        *[f"• {r}" for r in a.reasons],
        "",
        f"{fmt_price(a.entry_low)}-{fmt_price(a.entry_high)} giriş bölgesi",
        f"Kademeler:{'-'.join(fmt_price(t) for t in a.targets)}++",
        f"Stop:{fmt_price(a.stop)} altı",
        f"📉 Risk: %{(a.price - a.stop) / a.price * 100:.1f} · 🎯 TP1 +%{(a.targets[0] / a.price - 1) * 100:.1f}",
    ]
    if news:
        lines.append(f"📰 Haber: {news}")
    return with_footer(lines)


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
