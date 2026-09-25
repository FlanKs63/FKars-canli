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
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot import common
from bot import filtre
from bot.common import (env_float, env_int, env_str, gemini_text, redact, require_telegram_env,
                        run_main, send_telegram)
from bot.live import Cooldown, alert_message, detect, is_fresh, market_open
from bot.market_data import _extract
from bot import kap, komutlar, takvim, tatil
from bot.universe import BIST_STOCKS, CRYPTO, GYO, US_STOCKS
from zoneinfo import ZoneInfo

takvim_tz = ZoneInfo("Europe/Istanbul")

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


def download_1m(symbols: list[str], prepost: bool, period: str = "1d") -> dict[str, pd.DataFrame]:
    import yfinance as yf
    frames: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        try:
            data = yf.download(chunk, period=period, interval="1m", prepost=prepost, group_by="ticker",
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
    text = gemini_text(prompt, max_chars=260, search=True)
    if text or env_str("GOOGLE_NEWS", "1") == "0":
        return text
    from bot import google_news                       # Gemini kotası doldu / anahtar yok → Google Haberler
    code = symbol.removesuffix(".IS").removesuffix("-USD")
    query, lang = (f"{code} hisse", "tr") if symbol.endswith(".IS") else \
        ((display.lstrip("$"), "tr") if symbol.endswith("-USD") else (f"{code} stock", "en"))
    return google_news.summary(query, lang=lang, max_age_hours=24, max_chars=260)


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


def safe_send(send, text: str) -> bool:
    """Telegram hatası turun geri kalanını bozmasın."""
    try:
        send(text)
        return True
    except Exception as e:  # noqa: BLE001
        print(redact(f"  Telegram gönderilemedi: {str(e)[:150]}"))
        return False


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
        # kripto 7/24: gece yarısından sonra da 30+ mum olsun diye 2 günlük veri
        frames = fetch(list(wl), prepost=(market == "us"), period="2d" if market == "kripto" else "1d")
        stale = [s for s, d in frames.items() if not is_fresh(d, market, now)]
        if stale and len(stale) == len(frames):
            continue                               # tüm veri bayat (tatil / seans öncesi / Yahoo gecikmesi)
        for sym, df in frames.items():
            if sym in stale:
                continue
            alert = detect(df, sym, wl.get(sym, sym), market, MIN_DOLLAR_5M.get(market, 50_000))
            if alert is None or not cooldown.allow(sym, alert.price, now):
                continue
            last_reject = rejected.get(sym)
            if last_reject and (now - last_reject).total_seconds() < 300:
                continue                                   # az önce elendi/gönderilemedi; 5 dk boşuna uğraşma
            if not use_filters:
                news = ""
                if news_budget is not None and news_budget[0] > 0:
                    news = news_for(alert.display, sym)
                    news_budget[0] -= 1
                if not safe_send(send, alert_message(alert, news)):
                    rejected[sym] = now                    # gönderilemedi: 5 dk boyunca tekrar deneme
                    continue
            else:
                df5 = fetch5([sym], market).get(sym)
                local_day = str(now.astimezone(filtre.SESSIONS[market][0]).date()) if market in filtre.SESSIONS else ""
                av = avg_vol(sym, local_day) if market in filtre.SESSIONS else None
                res = filtre.evaluate(alert, df5, now, av)
                if not res["gonder"]:
                    rejected[sym] = now
                    print(f"  ATLANDI {sym}: {res['neden']}")
                    continue
                if not safe_send(send, filtre.filtered_message(alert, res, _news_line(alert, sym, now, news_budget))):
                    rejected[sym] = now                    # gönderilemedi: 5 dk boyunca tekrar deneme (haber kotası yenmesin)
                    continue
                tracker[sym] = filtre.open_from(alert, res, now)
            cooldown.mark(sym, alert.price, now)
            sent += 1
            print(f"  ALARM {sym}: {', '.join(alert.reasons)}")
    return sent


def track_open(tracker: dict, now: datetime, send=send_telegram, fetch5=None,
               results: list | None = None) -> int:
    """Açık sinyallerde çıkış koşuluna bakar; çıkış olursa '🔴 SAT' mesajı gönderir.
    Kapanan (çıkış ya da takip süresi dolan) her sinyalin sonucu results'a yazılır."""
    fetch5 = fetch5 or filtre.fetch_5m
    results = results if results is not None else []
    by_market: dict[str, list[str]] = {}
    for sym, sig in tracker.items():
        by_market.setdefault(sig.market, []).append(sym)
    exits = 0
    for market, syms in by_market.items():
        frames = fetch5(syms, market)
        for sym in syms:
            sig, df = tracker[sym], frames.get(sym)
            if filtre.expired(sig, now):
                last = float(df["Close"].iloc[-1]) if df is not None and len(df) else sig.entry
                filtre.record_result(results, sig, now, last, "süre doldu")
                print(f"  Takip süresi doldu: {sym}")
                tracker.pop(sym)
                continue
            if not market_open(market, now):
                continue
            msg = filtre.check_exit(sig, df, now)
            if msg:
                if not safe_send(send, msg):
                    continue
                filtre.record_result(results, sig, now, sig.exit_price or sig.entry, "çıkış")
                tracker.pop(sym)
                exits += 1
                print(f"  ÇIKIŞ {sym}")
    return exits


def load_extra(path) -> dict:
    """KAP'ta görülen bildirimler, Telegram update ofseti, takvimin son gönderildiği gün."""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}
    return (data.get("extra") or {}) if isinstance(data, dict) else {}


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


def save_state(path, cooldown: Cooldown, tracker: dict, extra: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"cooldown": cooldown.to_dict(), "open": {s: sig.to_dict() for s, sig in tracker.items()},
            "extra": extra or {}}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def calendar_symbols() -> tuple[dict[str, str], set[str]]:
    syms = {s: f"${s}" for s in US_STOCKS}
    syms.update({f"{s}.IS": f"${s}" for s in list(BIST_STOCKS) + list(GYO)})
    return syms, set(US_STOCKS)


def calendar_message(now: datetime) -> str | None:
    syms, us_watch = calendar_symbols()
    today = now.astimezone(takvim_tz).date()
    return takvim.message(takvim.collect(syms, today, days=env_int("TAKVIM_DAYS", 3), us_watch=us_watch), today)


class CalendarJob:
    """Takvim ~130 hisse için tek tek Yahoo'ya soruyor (1-3 dk). Alarm döngüsü donmasın diye arka planda
    hazırlanır; sonuç önbellekte tutulur, /takvim komutu önbelleği döndürür."""

    def __init__(self, build=None):
        self.build = build or calendar_message
        self.thread: threading.Thread | None = None
        self.result: tuple[str, str | None, bool] | None = None      # (gün, mesaj, başarılı)

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, now: datetime) -> None:
        if self.running():
            return
        day = str(now.astimezone(takvim_tz).date())

        def work():
            try:
                self.result = (day, self.build(now), True)
            except Exception as e:  # noqa: BLE001
                print(redact(f"  Takvim hazırlanamadı: {str(e)[:150]}"))
                self.result = (day, None, False)

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def take(self):
        res, self.result = self.result, None
        return res


calendar_job = CalendarJob()
NO_EVENTS = "📅 Önümüzdeki günlerde takip listesinde bilanço/temettü yok."


def command_handlers(now: datetime, markets: list[str], tracker: dict, extra: dict | None = None) -> dict:
    extra = extra if extra is not None else {}

    def fiyat(args):
        if not args:
            return "Kullanım: /fiyat THYAO  (ya da /fiyat AAPL, /fiyat BTC)"
        sym, disp = komutlar.resolve_symbol(args[0], set(BIST_STOCKS) | set(GYO), dict(CRYPTO))
        return komutlar.price_text(sym, disp)

    def takvim_cmd(args):
        cache = extra.get("takvim_cache") or {}
        if cache.get("date"):
            return cache.get("msg") or NO_EVENTS
        calendar_job.start(now)
        return "📅 Takvim hazırlanıyor, 1-2 dakika sonra /takvim yaz."

    return {
        "yardim": lambda args: "\n".join(komutlar.HELP),
        "durum": lambda args: komutlar.status_text(now, markets, market_open, tracker),
        "fiyat": fiyat,
        "takvim": takvim_cmd,
        "sonuc": lambda args: filtre.results_summary(
            extra.get("results") or [], str((now - timedelta(days=7)).astimezone(takvim_tz).date()),
            "⚡ CANLI ALARM — SON 7 GÜN") or "Son 7 günde kapanan canlı sinyal yok.",
    }


def side_tasks(now: datetime, markets: list[str], tracker: dict, extra: dict, send=send_telegram,
               job: CalendarJob | None = None) -> None:
    """Alarm turu dışındaki işler: Telegram komutları, KAP bildirimleri, günlük takvim."""
    job = job or calendar_job
    mono = time.monotonic()
    if env_str("TG_COMMANDS", "1") != "0":
        offset = int(extra.get("tg_offset") or 0)
        updates = komutlar.get_updates(offset)
        # ofset cevaplardan ÖNCE kaydedilir: gönderim hata verse de aynı komut ikinci kez cevaplanmaz
        extra["tg_offset"] = komutlar.next_offset(updates, offset) or offset or 1
        if offset:                                     # ilk açılışta eski komutlara cevap verme
            komutlar.handle(updates, env_str("TELEGRAM_CHAT_ID"),
                            command_handlers(now, markets, tracker, extra), lambda t: safe_send(send, t))
    now_tr = now.astimezone(takvim_tz)
    if (env_str("KAP_ALERTS", "1") != "0" and tatil.is_trading_day("bist", now_tr.date()) and 7 <= now_tr.hour <= 23
            and mono - _timers.get("kap", -1e9) >= max(env_int("KAP_POLL_SEC", 120), 60)):
        _timers["kap"] = mono
        seen = extra.get("kap_seen")
        if not isinstance(seen, dict):                 # eski liste biçimi → {no: gün}
            seen = {str(i): str(now_tr.date()) for i in (seen or [])}
            extra["kap_seen"] = seen
        kap.check(set(BIST_STOCKS) | set(GYO), seen, now_tr, lambda t: safe_send(send, t) or _raise(),
                  first_run=not seen, sent_log=extra.setdefault("kap_sent", []), since=extra.get("kap_last"))
        extra["kap_last"] = str(now_tr.date())
    # takvim: arka planda hazırlanır, bitince (bugün henüz gönderilmediyse) gönderilir
    res = job.take()
    if res:
        day, msg, ok = res
        if ok:
            extra["takvim_cache"] = {"date": day, "msg": msg}
            if extra.get("takvim_date") != day and takvim.due(now_tr, extra.get("takvim_date")):
                extra["takvim_date"] = day
                if msg:
                    safe_send(send, msg)
        else:
            _timers["takvim_fail"] = mono              # 30 dk sonra tekrar dene
    cache = extra.get("takvim_cache") or {}
    if cache.get("date") and cache["date"] != str(now_tr.date()):
        extra.pop("takvim_cache", None)                # dünün önbelleği
    # canlı alarm günlük sonuç özeti (23:30 TR, o gün kapanan sinyal varsa)
    today_s = str(now_tr.date())
    if (now_tr.hour * 60 + now_tr.minute >= env_int("RESULTS_HOUR", 23) * 60 + 30
            and extra.get("results_date") != today_s):
        extra["results_date"] = today_s
        text = filtre.results_summary(extra.get("results") or [], today_s, "⚡ CANLI ALARM — BUGÜNÜN SONUÇLARI")
        if text:
            safe_send(send, text)
    if (takvim.due(now_tr, extra.get("takvim_date")) and not job.running()
            and mono - _timers.get("takvim_fail", -1e9) >= 1800):
        job.start(now)


def _raise():
    raise RuntimeError("gönderilemedi")


def push_state(path) -> None:
    """GitHub Actions'ta durum dosyasını periyodik commit'le (çalışma yarıda kesilirse eski duruma dönülmesin)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    cmds = [
        ["git", "config", "user.name", "sinyal-bot"],
        ["git", "config", "user.email", "sinyal-bot@users.noreply.github.com"],
        ["git", "add", "state/"],
    ]
    try:
        for c in cmds:
            subprocess.run(c, check=True, capture_output=True, timeout=30)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], timeout=30).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", "Durum güncellendi: Canlı alarm (ara kayıt)"],
                       check=True, capture_output=True, timeout=30)
        for _ in range(3):
            pull = subprocess.run(["git", "pull", "--rebase", "-q"], capture_output=True, timeout=60)
            if pull.returncode == 0 and subprocess.run(["git", "push", "-q"], capture_output=True,
                                                       timeout=60).returncode == 0:
                print("  Durum ara kaydı gönderildi")
                return
            subprocess.run(["git", "rebase", "--abort"], capture_output=True, timeout=30)
            time.sleep(5)
        print("  Durum ara kaydı gönderilemedi (sonra tekrar denenecek)")
    except Exception as e:  # noqa: BLE001
        print(redact(f"  Durum ara kaydı hatası: {str(e)[:150]}"))


_timers: dict[str, float] = {}


def main() -> None:
    require_telegram_env()
    markets = [m.strip() for m in env_str("LIVE_MARKETS", "us,kripto,bist").split(",") if m.strip()]
    poll = max(env_int("LIVE_POLL_SECONDS", 30), 10)
    max_minutes = env_int("LIVE_MAX_MINUTES", 340)
    cooldown = Cooldown(env_float("LIVE_COOLDOWN_MIN", 45), env_float("LIVE_REARM_PCT", 0.05),
                        env_int("LIVE_MAX_PER_HOUR", 15))
    state_path = common.STATE_DIR / "live.json"
    saved_cooldown, tracker = load_state(state_path)
    extra = load_extra(state_path)
    cooldown.load(saved_cooldown)
    rejected: dict = {}
    exit_every = max(env_int("LIVE_EXIT_CHECK_SEC", 300), 60)
    exit_at = 0.0

    start = time.monotonic()
    pushed_at = time.monotonic()
    movers: dict[str, str] = {}
    movers_at = 0.0
    news_budget = [env_int("LIVE_NEWS_PER_HOUR", 6)]
    news_reset = time.monotonic()
    print(f"Canlı alarm başladı: {', '.join(markets)} · her {poll} sn")
    while True:
        now = datetime.now(timezone.utc)
        if "us" in markets and market_open("us", now) and time.monotonic() - movers_at > 300:
            movers_at = time.monotonic()
            try:
                movers = fetch_movers()
                print(f"  Yükselen/aktif liste: {len(movers)} hisse")
            except Exception as e:  # noqa: BLE001 - Yahoo bozuk cevap verse de döngü durmasın
                print(redact(f"  Yükselen listesi alınamadı: {str(e)[:150]}"))
        if time.monotonic() - news_reset > 3600:
            news_budget, news_reset = [env_int("LIVE_NEWS_PER_HOUR", 6)], time.monotonic()
            common.reset_ai_budget()
        try:
            run_once(markets, cooldown, movers, now, news_budget=news_budget,
                     tracker=tracker, rejected=rejected)
        except Exception as e:  # noqa: BLE001 - tek turdaki hata döngüyü durdurmasın
            print(redact(f"  Tur hatası: {e}"))
        if tracker and time.monotonic() - exit_at >= exit_every:
            exit_at = time.monotonic()
            try:
                track_open(tracker, now, results=extra.setdefault("results", []))
            except Exception as e:  # noqa: BLE001
                print(redact(f"  Takip hatası: {e}"))
        try:
            side_tasks(now, markets, tracker, extra)
        except Exception as e:  # noqa: BLE001
            print(redact(f"  Yan görev hatası: {e}"))
        save_state(state_path, cooldown, tracker, extra)
        if time.monotonic() - pushed_at >= env_int("LIVE_STATE_PUSH_MIN", 20) * 60:
            pushed_at = time.monotonic()
            push_state(state_path)
        if max_minutes and (time.monotonic() - start) / 60 >= max_minutes:
            print("Süre doldu, çıkılıyor (bir sonraki çalışma devam edecek).")
            break
        time.sleep(poll)


if __name__ == "__main__":
    run_main(BOT_NAME, main)
