"""
================================================================================
CANLI ALARM BOTU — ani hacim patlaması / sıçrama / zirve kırılımı
================================================================================

Sürekli döner; her LIVE_POLL_SECONDS saniyede 1 dakikalık mumlara bakar ve
yalnızca bir koşul oluşunca Telegram'a yazar. Aynı hisse için LIVE_COOLDOWN_MIN
dakika tekrar yazmaz (fiyat son alarmdan %X daha yükselirse yazar).

Piyasalar (LIVE_MARKETS):
  us      ABD hisseleri + o gün en çok yükselen / en aktif / küçük şirket listeleri
          (1 dolarlık hisseler dahil), ön piyasa 04:00 ET'den itibaren
  kripto  Bitcoin, Ethereum ve büyük altcoinler (7/24)
  bist    BIST hisseleri (Yahoo verisi ~15 dk gecikmeli; mesajda belirtilir)

Alarm tetiklenince (GEMINI_API_KEY varsa) o hisse için tek seferlik haber araştırması yapılır.

Ayarlar:
  LIVE_MARKETS=us,kripto,bist   LIVE_POLL_SECONDS=30     LIVE_MAX_MINUTES=340 (0 = sonsuz)
  LIVE_COOLDOWN_MIN=45          LIVE_REARM_PCT=0.05      LIVE_MAX_PER_HOUR=15
  LIVE_JUMP_PCT (hisse 0.03, kripto 0.02)   LIVE_VOLUME_MULT=5
  LIVE_MIN_PRICE=0.5  LIVE_MAX_PRICE=20   (yükselenler listesinden alınacak fiyat aralığı)
  LIVE_NEWS_PER_HOUR=6
================================================================================
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pandas as pd

from bot import common
from bot import filtre
from bot.common import (env_float, env_int, env_str, gemini_text, redact, require_telegram_env,
                        run_main, send_telegram)
from bot.live import Cooldown, alert_message, detect, market_open
from bot.market_data import _extract
from bot.universe import BIST_STOCKS, CRYPTO, US_STOCKS

BOT_NAME = "Canlı alarm"
MIN_DOLLAR_5M = {"us": 50_000, "kripto": 100_000, "bist": 500_000}


def fetch_movers() -> dict[str, str]:
    """Yahoo'nun o günkü listeleri: en çok yükselenler, en aktifler, küçük şirket yükselenleri."""
    lo, hi = env_float("LIVE_MIN_PRICE", 0.5), env_float("LIVE_MAX_PRICE", 20)
    out: dict[str, str] = {}
    try:
        import yfinance as yf
        screen = getattr(yf, "screen", None)
        if screen is None:
            return out
        for query in ("day_gainers", "most_actives", "small_cap_gainers"):
            try:
                res = screen(query, count=100)
            except Exception as e:  # noqa: BLE001
                print(f"  Liste alınamadı ({query}): {e}")
                continue
            for q in (res or {}).get("quotes", []):
                sym, price = q.get("symbol"), q.get("regularMarketPrice")
                if sym and price and lo <= float(price) <= hi and "." not in sym:
                    out[sym] = f"${sym}"
    except ImportError:
        pass
    return out


def download_1m(symbols: list[str], prepost: bool) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    frames: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        try:
            data = yf.download(chunk, period="1d", interval="1m", prepost=prepost, group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            print(f"  1 dk veri alınamadı: {e}")
            continue
        for sym in chunk:
            df = _extract(data, sym)
            if df is not None and len(df):
                frames[sym] = df
    return frames


def watchlist(market: str, movers: dict[str, str]) -> dict[str, str]:
    if market == "us":
        wl = {s: f"${s}" for s in US_STOCKS}
        wl.update(movers)
        return wl
    if market == "kripto":
        return dict(CRYPTO)
    if market == "bist":
        return {f"{s}.IS": f"${s}" for s in BIST_STOCKS}
    return {}


def news_for(display: str, symbol: str) -> str:
    prompt = (f"{display.lstrip('$')} ({symbol}) şu an sert yükseliyor. Son 24 saatteki haberleri, "
              "şirket açıklamalarını internette ara; yükselişin olası sebebini Türkçe en fazla 2 kısa "
              "cümleyle yaz. Haber yoksa sadece 'Belirgin bir haber yok.' yaz.")
    return gemini_text(prompt, max_chars=260, search=True)


def _news_line(alert, sym: str, now: datetime, news_budget: list[int] | None) -> str:
    """ABD hissesinde FINNHUB_KEY varsa Finnhub; yoksa (ve BIST/kripto'da) Gemini araştırması."""
    if alert.market == "us":
        fh = filtre.finnhub_news(sym, now)
        if fh is not None:
            return fh
    if news_budget is not None and news_budget[0] > 0:
        news_budget[0] -= 1
        text = news_for(alert.display, sym)
        return f"📰 Haber: {text}" if text else ""
    return ""


def run_once(markets: list[str], cooldown: Cooldown, movers: dict[str, str], now: datetime,
             fetch=download_1m, send=send_telegram, news_budget: list[int] | None = None,
             tracker: dict | None = None, rejected: dict | None = None,
             fetch5=None, avg_vol=None) -> int:
    """Bir tur: her açık piyasada alarm arar. Filtreler açıksa (LIVE_FILTERS≠0) alarm
    sinyal_filtreleri'nden geçmeden gönderilmez; geçen sinyal tracker'a eklenir."""
    fetch5 = fetch5 or filtre.fetch_5m
    avg_vol = avg_vol or filtre.avg_daily_volume
    tracker = tracker if tracker is not None else {}
    rejected = rejected if rejected is not None else {}
    use_filters = filtre.enabled()
    sent = 0
    for market in markets:
        if not market_open(market, now):
            continue
        wl = watchlist(market, movers)
        frames = fetch(list(wl), prepost=(market == "us"))
        for sym, df in frames.items():
            alert = detect(df, sym, wl.get(sym, sym), market, MIN_DOLLAR_5M.get(market, 50_000))
            if alert is None or not cooldown.allow(sym, alert.price, now):
                continue
            if not use_filters:
                news = ""
                if news_budget is not None and news_budget[0] > 0:
                    news = news_for(alert.display, sym)
                    news_budget[0] -= 1
                send(alert_message(alert, news))
            else:
                last_reject = rejected.get(sym)
                if last_reject and (now - last_reject).total_seconds() < 300:
                    continue                       # az önce elendi; 5 dk boşuna veri indirme
                df5 = fetch5([sym], market).get(sym)
                av = avg_vol(sym, str(now.date())) if market in filtre.SESSIONS else None
                res = filtre.evaluate(alert, df5, now, av)
                if not res["gonder"]:
                    rejected[sym] = now
                    print(f"  ATLANDI {sym}: {res['neden']}")
                    continue
                send(filtre.filtered_message(alert, res, _news_line(alert, sym, now, news_budget)))
                tracker[sym] = filtre.open_from(alert, res, now)
            cooldown.mark(sym, alert.price, now)
            sent += 1
            print(f"  ALARM {sym}: {', '.join(alert.reasons)}")
    return sent


def track_open(tracker: dict, now: datetime, send=send_telegram, fetch5=None) -> int:
    """Açık sinyallerde çıkış koşuluna bakar; çıkış olursa '🚪 ÇIKIŞ' mesajı gönderir."""
    fetch5 = fetch5 or filtre.fetch_5m
    for sym in [s for s, sig in tracker.items() if filtre.expired(sig, now)]:
        print(f"  Takip süresi doldu: {sym}")
        tracker.pop(sym)
    by_market: dict[str, list[str]] = {}
    for sym, sig in tracker.items():
        if market_open(sig.market, now):
            by_market.setdefault(sig.market, []).append(sym)
    exits = 0
    for market, syms in by_market.items():
        frames = fetch5(syms, market)
        for sym in syms:
            msg = filtre.check_exit(tracker[sym], frames.get(sym))
            if msg:
                send(msg)
                tracker.pop(sym)
                exits += 1
                print(f"  ÇIKIŞ {sym}")
    return exits


def load_state(path) -> tuple[dict, dict]:
    """state/live.json → (cooldown sözlüğü, açık sinyaller). Eski biçim (yalnız cooldown) de okunur."""
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}, {}
    if isinstance(data, dict) and "cooldown" in data:
        opened = {}
        for s, d in (data.get("open") or {}).items():
            try:
                opened[s] = filtre.OpenSignal.from_dict(d)
            except TypeError:
                continue
        return data.get("cooldown") or {}, opened
    return data if isinstance(data, dict) else {}, {}


def save_state(path, cooldown: Cooldown, tracker: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"cooldown": cooldown.to_dict(), "open": {s: sig.to_dict() for s, sig in tracker.items()}}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    require_telegram_env()
    markets = [m.strip() for m in env_str("LIVE_MARKETS", "us,kripto,bist").split(",") if m.strip()]
    poll = max(env_int("LIVE_POLL_SECONDS", 30), 10)
    max_minutes = env_int("LIVE_MAX_MINUTES", 340)
    cooldown = Cooldown(env_float("LIVE_COOLDOWN_MIN", 45), env_float("LIVE_REARM_PCT", 0.05),
                        env_int("LIVE_MAX_PER_HOUR", 15))
    state_path = common.STATE_DIR / "live.json"
    saved_cooldown, tracker = load_state(state_path)
    cooldown.load(saved_cooldown)
    rejected: dict = {}
    exit_every = max(env_int("LIVE_EXIT_CHECK_SEC", 300), 60)
    exit_at = 0.0

    start = time.monotonic()
    movers: dict[str, str] = {}
    movers_at = 0.0
    news_budget = [env_int("LIVE_NEWS_PER_HOUR", 6)]
    news_reset = time.monotonic()
    print(f"Canlı alarm başladı: {', '.join(markets)} · her {poll} sn")
    while True:
        now = datetime.now(timezone.utc)
        if "us" in markets and market_open("us", now) and time.monotonic() - movers_at > 300:
            movers = fetch_movers()
            movers_at = time.monotonic()
            print(f"  Yükselen/aktif liste: {len(movers)} hisse")
        if time.monotonic() - news_reset > 3600:
            news_budget, news_reset = [env_int("LIVE_NEWS_PER_HOUR", 6)], time.monotonic()
        try:
            run_once(markets, cooldown, movers, now, news_budget=news_budget,
                     tracker=tracker, rejected=rejected)
        except Exception as e:  # noqa: BLE001 - tek turdaki hata döngüyü durdurmasın
            print(redact(f"  Tur hatası: {e}"))
        if tracker and time.monotonic() - exit_at >= exit_every:
            exit_at = time.monotonic()
            try:
                track_open(tracker, now)
            except Exception as e:  # noqa: BLE001
                print(redact(f"  Takip hatası: {e}"))
        save_state(state_path, cooldown, tracker)
        if max_minutes and (time.monotonic() - start) / 60 >= max_minutes:
            print("Süre doldu, çıkılıyor (bir sonraki çalışma devam edecek).")
            break
        time.sleep(poll)


if __name__ == "__main__":
    run_main(BOT_NAME, main)
