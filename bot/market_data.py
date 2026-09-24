"""Yahoo Finance'ten günlük veri indirme ve yarım (kapanmamış) mumu ayıklama."""

from __future__ import annotations

import time
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .universe import GRAM_SERIES, TROY_OUNCE_GRAMS

LOOKBACK_PERIOD = "2y"     # EMA200'ün düzgün oturması için 1 yıl yetersiz
CHUNK_SIZE = 40            # tek seferde çok sembol istemek Yahoo'da hata artırıyor
RETRY_ATTEMPTS = 3

NY_TZ = ZoneInfo("America/New_York")
TR_TZ = ZoneInfo("Europe/Istanbul")

# Piyasa açıkken son günlük mum henüz kapanmamıştır; indikatörleri bozmasın diye atılır.
SESSION = {
    "hisse": (NY_TZ, dtime(9, 30), dtime(16, 15)),
    "bist": (TR_TZ, dtime(9, 55), dtime(18, 15)),
    "endeks": (TR_TZ, dtime(9, 55), dtime(18, 15)),
    "byf": (TR_TZ, dtime(9, 55), dtime(18, 15)),
    "gyo": (TR_TZ, dtime(9, 55), dtime(18, 15)),
}
ALWAYS_OPEN = {"emtia", "doviz", "kripto"}   # ~24 saat işlem gören piyasalar


def _last_bar_date(df: pd.DataFrame):
    return pd.Timestamp(df.index[-1]).date()


def drop_incomplete_bar(df: pd.DataFrame, asset_class: str, now_utc: datetime | None = None) -> pd.DataFrame:
    """Henüz kapanmamış bugünkü mumu siler."""
    if df.empty:
        return df
    now_utc = now_utc or datetime.now(timezone.utc)
    last = _last_bar_date(df)

    if asset_class in ALWAYS_OPEN:
        if last >= now_utc.date():
            return df.iloc[:-1]
        return df

    session = SESSION.get(asset_class)
    if not session:
        return df
    tz, open_t, close_t = session
    local = now_utc.astimezone(tz)
    if local.weekday() < 5 and open_t <= local.time() < close_t and last >= local.date():
        return df.iloc[:-1]
    return df


def _extract(data: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """yfinance çıktısından tek sembolün tablosunu çıkarır (sürüm farklarına dayanıklı)."""
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        for level in range(data.columns.nlevels):
            if symbol in data.columns.get_level_values(level):
                df = data.xs(symbol, axis=1, level=level)
                break
        else:
            return None
    else:
        df = data
    df = df.dropna(how="all")
    if "Close" not in df.columns or df["Close"].dropna().empty:
        return None
    return df.dropna(subset=["Close"])


def _download(symbols: list[str], period: str = LOOKBACK_PERIOD) -> pd.DataFrame:
    import yfinance as yf  # geç import: testlerde gerekmesin

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            data = yf.download(
                symbols, period=period, interval="1d", group_by="ticker",
                auto_adjust=True, threads=True, progress=False,
            )
            if data is not None and not data.empty:
                return data
        except Exception as e:  # noqa: BLE001
            print(f"  İndirme hatası ({attempt}/{RETRY_ATTEMPTS}): {e}")
        time.sleep(5 * attempt)
    return pd.DataFrame()


def download_many(symbols: list[str], period: str = LOOKBACK_PERIOD) -> dict[str, pd.DataFrame]:
    """Sembolleri parça parça indirir; eksik kalanları tek tek bir kez daha dener."""
    result: dict[str, pd.DataFrame] = {}
    unique = list(dict.fromkeys(symbols))
    for i in range(0, len(unique), CHUNK_SIZE):
        chunk = unique[i:i + CHUNK_SIZE]
        data = _download(chunk, period)
        for sym in chunk:
            df = _extract(data, sym)
            if df is not None:
                result[sym] = df

    missing = [s for s in unique if s not in result]
    for sym in missing:
        df = _extract(_download([sym], period), sym)
        if df is not None:
            result[sym] = df
    still_missing = [s for s in unique if s not in result]
    if still_missing:
        print(f"UYARI: veri alınamayan semboller (atlandı): {', '.join(still_missing)}")
    return result


def _day_index(index) -> pd.DatetimeIndex:
    """Saat dilimli/dilimsiz tarih indeksini sade gün indeksine çevirir."""
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def build_gram_series(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Ons fiyatını USD/TRY ile çarpıp gram TL serisine çevirir."""
    out: dict[str, pd.DataFrame] = {}
    fx = frames.get("USDTRY=X")
    if fx is None:
        return out
    fx_close = fx["Close"].copy()
    fx_close.index = _day_index(fx_close.index)

    for name, (_, ounce_symbol) in GRAM_SERIES.items():
        ounce = frames.get(ounce_symbol)
        if ounce is None:
            continue
        o = ounce[["High", "Low", "Close"]].copy()
        o.index = _day_index(o.index)
        joined = o.join(fx_close.rename("FX"), how="inner").dropna()
        if joined.empty:
            continue
        gram = pd.DataFrame(index=joined.index)
        for col in ("High", "Low", "Close"):
            gram[col] = joined[col] * joined["FX"] / TROY_OUNCE_GRAMS
        gram["Volume"] = 0.0
        out[name] = gram
    return out
