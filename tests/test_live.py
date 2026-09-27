"""Canlı alarm testleri (1 dakikalık sahte veriyle, internet gerektirmez)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DRY_RUN"] = "1"
os.environ.pop("GEMINI_API_KEY", None)
os.environ["GOOGLE_NEWS"] = "0"      # testlerde internete çıkma
os.environ["GERI_ALIM"] = "0"         # özet sadece ilgili testlerde açılır

from bot import filtre, geri_alim, kap, komutlar, live, takvim  # noqa: E402
import live_alerts  # noqa: E402


def minute_bars(n=120, price=1.0, spike=False, seed=0, end="2026-09-24 15:00"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=pd.Timestamp(end, tz="America/New_York"), periods=n, freq="1min")
    close = price * np.cumprod(1 + rng.normal(0, 0.001, n))
    vol = rng.uniform(80_000, 120_000, n)
    if spike:
        close[-5:] = close[-6] * np.array([1.02, 1.04, 1.06, 1.08, 1.10])
        vol[-5:] = vol[-5:] * 10
    df = pd.DataFrame({"Open": close, "Close": close, "Volume": vol}, index=idx)
    df["High"] = df["Close"] * 1.002
    df["Low"] = df["Close"] * 0.998
    df.iloc[0, df.columns.get_loc("Open")] = price
    return df


class TestDetect(unittest.TestCase):
    def test_spike_triggers(self):
        a = live.detect(minute_bars(spike=True), "XYZ", "$XYZ", "us", 50_000)
        self.assertIsNotNone(a)
        self.assertGreater(a.vol_mult, 5)
        self.assertAlmostEqual(a.jump5, 0.10, places=2)
        self.assertTrue(a.breakout)
        self.assertEqual(len(a.reasons), 3)
        self.assertLess(a.stop, a.entry_low)
        self.assertLess(a.entry_high, a.targets[0])

    def test_quiet_market_no_alert(self):
        self.assertIsNone(live.detect(minute_bars(), "XYZ", "$XYZ", "us", 50_000))

    def test_low_dollar_volume_ignored(self):
        df = minute_bars(spike=True)
        df["Volume"] = df["Volume"] / 10_000
        self.assertIsNone(live.detect(df, "XYZ", "$XYZ", "us", 50_000))

    def test_short_data_ignored(self):
        self.assertIsNone(live.detect(minute_bars(n=20, spike=True), "XYZ", "$XYZ", "us", 50_000))

    def test_message(self):
        a = live.detect(minute_bars(spike=True), "XYZ", "$XYZ", "us", 50_000)
        msg = live.alert_message(a, news="Şirket yeni anlaşma duyurdu.")
        self.assertTrue(msg.startswith("🟢 $XYZ AL"))
        for part in ("🔹 Giriş:", "🛑 Stop:", "🎯 Kademeler:", " → ", "⚡ Zirve kırıldı · hacim",
                     "📰 Haber: Şirket", "(Furkanla Katla)"):
            self.assertIn(part, msg)
        a.delayed = True
        self.assertIn("15 dk gecikmeli", live.alert_message(a))
        a.market = "bist"
        self.assertIn("₺", live.alert_message(a))


class TestHolidaysAndFreshness(unittest.TestCase):
    def test_holidays(self):
        from datetime import date
        from bot import tatil
        self.assertTrue(tatil.is_holiday("bist", date(2026, 10, 29)))
        self.assertTrue(tatil.is_holiday("us", date(2026, 11, 26)))
        self.assertFalse(tatil.is_trading_day("bist", date(2026, 5, 27)))          # Kurban Bayramı
        self.assertTrue(tatil.is_trading_day("kripto", date(2026, 10, 29)))
        self.assertIsNotNone(tatil.early_close("bist", date(2026, 10, 28)))
        t = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)  # noqa: E731
        self.assertFalse(live.market_open("bist", t("2026-10-29T09:00")))          # Cumhuriyet Bayramı
        self.assertFalse(live.market_open("us", t("2026-11-26T15:00")))            # Thanksgiving
        self.assertFalse(live.market_open("bist", t("2026-10-28T10:30")))          # yarım gün 12:50'de bitti
        with mock.patch.dict(os.environ, {"EXTRA_HOLIDAYS_BIST": "2026-09-24"}):
            self.assertFalse(live.market_open("bist", t("2026-09-24T08:00")))

    def test_stale_data_no_alert(self):
        frames = {"XYZ": minute_bars(spike=True)}                 # 15:00 ET'de biten mumlar
        send = mock.Mock()
        later = datetime(2026, 9, 24, 19, 30, tzinfo=timezone.utc)   # 30 dk sonra → bayat
        with mock.patch.object(live_alerts, "watchlist", return_value={"XYZ": "$XYZ"}), \
                mock.patch.dict(os.environ, {"LIVE_FILTERS": "0"}):
            n = live_alerts.run_once(["us"], live.Cooldown(45, 0.05, 15), {}, later,
                                     fetch=mock.Mock(return_value=frames), send=send)
        self.assertEqual(n, 0)
        send.assert_not_called()


class TestSessionsAndCooldown(unittest.TestCase):
    def test_market_open(self):
        t = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)  # noqa: E731
        self.assertTrue(live.market_open("us", t("2026-09-24T09:00")))     # 05:00 ET ön piyasa
        self.assertFalse(live.market_open("us", t("2026-09-24T02:00")))    # 22:00 ET
        self.assertFalse(live.market_open("us", t("2026-09-26T15:00")))    # cumartesi
        self.assertTrue(live.market_open("bist", t("2026-09-24T08:00")))   # 11:00 TR
        self.assertFalse(live.market_open("bist", t("2026-09-24T16:00")))  # 19:00 TR
        self.assertTrue(live.market_open("kripto", t("2026-09-26T03:00")))

    def test_cooldown(self):
        cd = live.Cooldown(minutes=45, rearm_pct=0.05, max_per_hour=2)
        now = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
        self.assertTrue(cd.allow("A", 1.0, now))
        cd.mark("A", 1.0, now)
        self.assertFalse(cd.allow("A", 1.02, now + timedelta(minutes=10)))   # bekleme süresi
        self.assertTrue(cd.allow("A", 1.06, now + timedelta(minutes=10)))    # %5+ yeni yükseliş
        self.assertTrue(cd.allow("A", 1.0, now + timedelta(minutes=50)))     # süre doldu
        cd.mark("B", 2.0, now)
        self.assertFalse(cd.allow("C", 3.0, now))                            # saatlik sınır
        restored = live.Cooldown(45, 0.05, 2)
        restored.load(cd.to_dict())
        self.assertFalse(restored.allow("A", 1.01, now + timedelta(minutes=5)))


class TestLoop(unittest.TestCase):
    def test_run_once_sends_once_per_spike(self):
        now = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)      # 15:00 ET
        frames = {"XYZ": minute_bars(spike=True), "AAPL": minute_bars(price=200, seed=2)}
        fetch = mock.Mock(return_value=frames)
        send = mock.Mock()
        cd = live.Cooldown(45, 0.05, 15)
        with mock.patch.object(live_alerts, "watchlist", return_value={"XYZ": "$XYZ", "AAPL": "$AAPL"}), \
                mock.patch.dict(os.environ, {"LIVE_FILTERS": "0"}):
            n1 = live_alerts.run_once(["us"], cd, {}, now, fetch=fetch, send=send)
            n2 = live_alerts.run_once(["us"], cd, {}, now + timedelta(seconds=30), fetch=fetch, send=send)
        self.assertEqual((n1, n2), (1, 0))
        self.assertIn("$XYZ", send.call_args[0][0])

    def test_closed_market_skipped(self):
        fetch = mock.Mock(return_value={})
        now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)      # cumartesi
        live_alerts.run_once(["us", "bist"], live.Cooldown(45, 0.05, 15), {}, now, fetch=fetch, send=mock.Mock())
        fetch.assert_not_called()

    def test_movers_filter(self):
        fake = mock.Mock()
        fake.screen = mock.Mock(return_value={"quotes": [
            {"symbol": "PENY", "regularMarketPrice": 1.2},
            {"symbol": "BIG", "regularMarketPrice": 450.0},
            {"symbol": "ABC.TO", "regularMarketPrice": 2.0}]})
        with mock.patch.dict(sys.modules, {"yfinance": fake}):
            movers = live_alerts.fetch_movers()
        self.assertEqual(movers, {"PENY": "$PENY"})

    def test_watchlist_includes_movers(self):
        wl = live_alerts.watchlist("us", {"PENY": "$PENY"})
        self.assertIn("PENY", wl)
        self.assertIn("AAPL", wl)
        self.assertIn("BTC-USD", live_alerts.watchlist("kripto", {}))
        self.assertIn("THYAO.IS", live_alerts.watchlist("bist", {}))


def five_min(days=2, per_day=78, price=10.0, spike=False, end="2026-09-24 15:00", seed=1):
    """ABD saatine göre 5 dk mumlar (iki gün)."""
    rng = np.random.default_rng(seed)
    frames = []
    end_ts = pd.Timestamp(end, tz="America/New_York")
    for d in range(days):
        day_end = end_ts - pd.Timedelta(days=days - 1 - d)
        idx = pd.date_range(end=day_end, periods=per_day, freq="5min")
        close = price * np.cumprod(1 + rng.normal(0, 0.002, per_day))
        vol = rng.uniform(20_000, 30_000, per_day)
        frames.append(pd.DataFrame({"Open": close, "High": close * 1.003, "Low": close * 0.997,
                                    "Close": close, "Volume": vol}, index=idx))
    df = pd.concat(frames)
    if spike:
        df.iloc[-1, df.columns.get_loc("Close")] = df["Close"].iloc[-2] * 1.04
        df.iloc[-1, df.columns.get_loc("High")] = df["Close"].iloc[-1] * 1.001
        df.iloc[-1, df.columns.get_loc("Volume")] = 300_000
    return df


class TestFilters(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)     # 15:00 ET

    def _run(self, eval_result=None, news=None, frames5=None):
        frames = {"XYZ": minute_bars(spike=True)}
        send = mock.Mock()
        tracker, rejected = {}, {}
        fetch5 = mock.Mock(return_value=frames5 if frames5 is not None else {"XYZ": five_min(spike=True)})
        patches = [mock.patch.object(live_alerts, "watchlist", return_value={"XYZ": "$XYZ"}),
                   mock.patch.dict(os.environ, {"LIVE_FILTERS": "1"}),
                   mock.patch.object(filtre, "finnhub_news", return_value=news)]
        if eval_result is not None:
            patches.append(mock.patch.object(filtre.sf, "sinyal_degerlendir", return_value=eval_result))
        for p in patches:
            p.start()
        try:
            n = live_alerts.run_once(["us"], live.Cooldown(45, 0.05, 15), {}, self.NOW,
                                     fetch=mock.Mock(return_value=frames), send=send, tracker=tracker,
                                     rejected=rejected, fetch5=fetch5, avg_vol=lambda s, d: 1_000_000)
        finally:
            for p in reversed(patches):
                p.stop()
        return n, send, tracker, rejected, fetch5

    def test_rejected_alert_not_sent(self):
        n, send, tracker, rejected, _ = self._run()          # günlük RVOL ~ düşük → elenir
        self.assertEqual(n, 0)
        send.assert_not_called()
        self.assertIn("XYZ", rejected)
        self.assertEqual(tracker, {})

    def test_passed_alert_uses_filter_levels_and_is_tracked(self):
        ok = {"gonder": True, "neden": "tüm filtreler geçti", "stop": 9.5, "hedefler": [11.0, 12.0, 13.0],
              "mesaj_ek": "\nGiriş: 10.00 (VWAP 9.80)\nKademeler: 11.00 - 12.00 - 13.00\nStop: 9.50 altı"}
        n, send, tracker, _, _ = self._run(eval_result=ok, news="📰 Haber: Şirket FDA onayı aldı")
        self.assertEqual(n, 1)
        msg = send.call_args[0][0]
        self.assertTrue(msg.startswith("🟢 $XYZ AL · GÜÇLÜ"))
        self.assertIn("🎯 Kademeler: 11.00$ → 12.00$ → 13.00$", msg)
        self.assertNotIn("ANİ HAREKET", msg)                    # eski uzun biçim yok
        self.assertNotIn("Plan:", msg)
        self.assertIn("📰 Haber: Şirket FDA onayı aldı", msg)
        self.assertIn("(Furkanla Katla)", msg)
        self.assertEqual(tracker["XYZ"].stop, 9.5)
        self.assertEqual(tracker["XYZ"].tp1, 11.0)

    def test_recent_reject_skips_download(self):
        n, send, tracker, rejected, fetch5 = self._run()
        fetch5.reset_mock()
        with mock.patch.object(live_alerts, "watchlist", return_value={"XYZ": "$XYZ"}), \
                mock.patch.dict(os.environ, {"LIVE_FILTERS": "1"}):
            live_alerts.run_once(["us"], live.Cooldown(45, 0.05, 15), {}, self.NOW + timedelta(minutes=2),
                                 fetch=mock.Mock(return_value={"XYZ": minute_bars(spike=True)}),
                                 send=send, rejected=rejected, fetch5=fetch5, avg_vol=lambda s, d: 1.0)
        fetch5.assert_not_called()

    def test_policy_hard_and_soft(self):
        base = {"mesaj_ek": "\nGiriş: 1\nHacim: 2x · Sinyal: ZAYIF", "stop": 1, "hedefler": [2]}
        ok = filtre.apply_policy({**base, "neden": "tüm filtreler geçti"})
        self.assertTrue(ok["gonder"])
        self.assertIn("Sinyal: GÜÇLÜ ✅", ok["mesaj_ek"])
        one_soft = filtre.apply_policy({**base, "neden": "günlük RVOL düşük (2.6x < 5x)"})
        self.assertTrue(one_soft["gonder"])
        self.assertIn("ORTA ⚠️ (günlük RVOL düşük", one_soft["mesaj_ek"])
        two_soft = filtre.apply_policy({**base, "neden": "günlük RVOL düşük (2x < 5x); TP1’den önce direnç var (önü kapalı)"})
        self.assertFalse(two_soft["gonder"])
        hard = filtre.apply_policy({**base, "neden": "fiyat VWAP altında"})
        self.assertFalse(hard["gonder"])
        self.assertFalse(filtre.apply_policy({"gonder": False, "neden": "yetersiz veri"})["gonder"])
        with mock.patch.dict(os.environ, {"LIVE_MAX_SOFT": "0"}):              # arkadaşın katı modu
            self.assertFalse(filtre.apply_policy({**base, "neden": "günlük RVOL düşük (2x < 5x)"})["gonder"])

    def test_short_buy_message(self):
        a = live.detect(minute_bars(spike=True), "XYZ", "$XYZ", "us", 50_000)
        stop = a.price * 0.95
        res = {"stop": stop, "hedefler": [a.price * k for k in (1.075, 1.125, 1.2)], "kirilan": a.prior_high,
               "esnek": ["günlük RVOL düşük (1.0x < 5x)"]}
        with mock.patch.dict(os.environ, {"LIVE_KASA": "1000"}):
            msg = filtre.filtered_message(a, res, "📰 Haber: " + "uzun " * 60)
        lines = msg.split("\n")
        self.assertTrue(lines[0].startswith("🟡 $XYZ AL · ORTA"))
        self.assertIn("⚠️ Dikkat: günlük hacim zayıf", msg)
        self.assertIn("💰 1000$ kasa:", msg)
        self.assertIn("🛑 Stop: ", msg)
        self.assertIn("(%-5.0)", msg)
        self.assertTrue(all(len(line) <= 160 for line in lines))           # uzun satır yok
        low, high = filtre.entry_zone(a.price, stop, a.prior_high)
        self.assertLessEqual(low, a.price)
        self.assertGreater(high, a.price)
        self.assertGreaterEqual(low, min(a.prior_high, a.price))            # kırılan seviyenin altına inmez
        self.assertEqual(filtre.entry_zone(10.0, 9.0), (9.75, 10.15))

    def test_daily_rvol_and_prev_high(self):
        df = five_min()
        rv = filtre.daily_rvol(df, "us", self.NOW, avg_vol=1_000_000)
        self.assertIsNotNone(rv)
        self.assertLess(rv, 5)
        self.assertIsNone(filtre.daily_rvol(df, "kripto", self.NOW, 1_000_000))
        self.assertIsNotNone(filtre.previous_day_high(df, "us"))
        self.assertEqual(filtre.min_rvol("bist"), 3.0)

    def test_finnhub(self):
        now = self.NOW
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=200, json=mock.Mock(return_value=[
            {"headline": "Eski haber", "datetime": now.timestamp() - 3 * 86400},
            {"headline": "Yeni sözleşme", "datetime": now.timestamp() - 3600}]))
        os.environ.pop("FINNHUB_KEY", None)
        self.assertIsNone(filtre.finnhub_news("XYZ", now, session))       # anahtar yok → Gemini'ye düşer
        with mock.patch.dict(os.environ, {"FINNHUB_KEY": "fh_test"}):
            self.assertEqual(filtre.finnhub_news("XYZ", now, session), "📰 Haber: Yeni sözleşme")
            session.get.return_value.json.return_value = []
            self.assertEqual(filtre.finnhub_news("XYZ", now, session), "📰 Haber yok (dikkat)")
            self.assertEqual(session.get.call_args.kwargs["params"]["token"], "fh_test")


class TestExitTracking(unittest.TestCase):
    def _sig(self, df, **kw):
        opened = df.index[-10].tz_convert("UTC").isoformat()
        base = dict(symbol="XYZ", display="$XYZ", market="us", entry=10.0, stop=9.0, tp1=11.0,
                    kirilan=9.5, opened=opened)
        base.update(kw)
        return filtre.OpenSignal(**base)

    def test_stop_exit(self):
        df = five_min(price=10.0)
        df.iloc[-2, df.columns.get_loc("Low")] = 8.5
        msg = filtre.check_exit(self._sig(df), df)
        self.assertIn("🔴 $XYZ SAT — stop çalıştı", msg)
        self.assertIn("%-10.0", msg)

    def test_exit_labels_short(self):
        df = five_min(price=10.0)
        for warn, head in (("Fiyat kırılan seviyenin altına döndü — sahte kırılım, stopu beklemeden çık.",
                            "🔴 $XYZ SAT — sahte kırılım"),
                           ("Yükseliş hacimsiz devam ediyor — pozisyonun yarısını azalt.",
                            "🟠 $XYZ YARISINI SAT — hacim sönüyor"),
                           ("Hacimli doji oluştu — kâr al / stopu yukarı çek.", "🟠 $XYZ KÂR AL — tepede hacimli doji")):
            with mock.patch.object(filtre.sf, "cikis_uyarisi", return_value=warn):
                msg = filtre.check_exit(self._sig(df, stop=1.0), df)
            self.assertTrue(msg.startswith(head), msg)
            self.assertIn("\nGiriş 10.00 → ", msg)

    def test_tp1_moves_stop_to_entry(self):
        df = five_min(price=10.0)
        df.iloc[-3, df.columns.get_loc("High")] = 11.2
        sig = self._sig(df, stop=5.0, kirilan=1.0)
        with mock.patch.object(filtre.sf, "cikis_uyarisi", return_value=None):
            self.assertIsNone(filtre.check_exit(sig, df))
        self.assertTrue(sig.tp1_hit)
        self.assertGreaterEqual(sig.stop, 10.0)

    def test_warning_exit_and_expiry(self):
        df = five_min(price=10.0)
        sig = self._sig(df, stop=1.0)
        with mock.patch.object(filtre.sf, "cikis_uyarisi", return_value="Mum VWAP altında kapandı — çıkış sinyali."):
            msg = filtre.check_exit(sig, df)
        self.assertIn("🔴 $XYZ SAT — VWAP altına indi", msg)
        old = filtre.OpenSignal("A", "$A", "us", 1, 0.9, 1.1, 1, "2026-09-24T00:00:00+00:00")
        self.assertTrue(filtre.expired(old, datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc)))

    def test_partial_bar_dropped(self):
        df = five_min(price=10.0)
        last = df.index[-1].tz_convert("UTC").to_pydatetime()
        self.assertEqual(len(filtre.closed_bars(df, last + timedelta(minutes=2))), len(df) - 1)   # oluşuyor
        self.assertEqual(len(filtre.closed_bars(df, last + timedelta(minutes=6))), len(df))       # kapandı

    def test_track_open_sends_and_removes(self):
        df = five_min(price=10.0)
        df.iloc[-2, df.columns.get_loc("Low")] = 8.5
        now = datetime(2026, 9, 24, 19, 5, tzinfo=timezone.utc)
        sig = self._sig(df)
        sig.opened = (now - timedelta(minutes=40)).isoformat()
        tracker = {"XYZ": sig}
        send = mock.Mock()
        n = live_alerts.track_open(tracker, now, send=send, fetch5=mock.Mock(return_value={"XYZ": df}))
        self.assertEqual(n, 1)
        self.assertEqual(tracker, {})
        self.assertIn("SAT —", send.call_args[0][0])

    def test_results_recorded_and_summarized(self):
        df = five_min(price=10.0)
        df.iloc[-2, df.columns.get_loc("Low")] = 8.5
        now = datetime(2026, 9, 24, 19, 5, tzinfo=timezone.utc)
        sig = self._sig(df)
        sig.opened = (now - timedelta(minutes=40)).isoformat()
        old = self._sig(df, symbol="OLD", display="$OLD")
        old.opened = (now - timedelta(hours=7)).isoformat()                    # takip süresi doldu
        tracker, results = {"XYZ": sig, "OLD": old}, []
        live_alerts.track_open(tracker, now, send=mock.Mock(),
                               fetch5=mock.Mock(return_value={"XYZ": df, "OLD": df}), results=results)
        self.assertEqual(tracker, {})
        self.assertEqual({r["reason"] for r in results}, {"çıkış", "süre doldu"})
        self.assertAlmostEqual([r for r in results if r["reason"] == "çıkış"][0]["pct"], -0.1, places=3)
        text = filtre.results_summary(results, "2026-09-24", "⚡ TEST")
        self.assertIn("Kapanan sinyal: 2", text)
        self.assertIsNone(filtre.results_summary(results, "2026-09-30", "x"))
        h = live_alerts.command_handlers(now, ["us"], {}, {"results": results})
        self.assertIn("SON 7 GÜN", h["sonuc"]([]))

    def test_state_roundtrip_and_old_format(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "live.json"
            path.write_text('{"AAA": {"time": "2026-09-24T10:00:00+00:00", "price": 1.0}}', encoding="utf-8")
            cd, opened = live_alerts.load_state(path)                   # eski biçim
            self.assertIn("AAA", cd)
            self.assertEqual(opened, {})
            cooldown = live.Cooldown(45, 0.05, 15)
            cooldown.load(cd)
            sig = filtre.OpenSignal("XYZ", "$XYZ", "us", 10, 9, 11, 9.5, "2026-09-24T19:00:00+00:00")
            live_alerts.save_state(path, cooldown, {"XYZ": sig})
            cd2, opened2 = live_alerts.load_state(path)
            self.assertIn("AAA", cd2)
            self.assertEqual(opened2["XYZ"].tp1, 11)


class TestKap(unittest.TestCase):
    ITEMS = [
        {"disclosureIndex": 101, "kapTitle": "TÜRK HAVA YOLLARI A.O.", "subject": "Özel Durum Açıklaması (Genel)",
         "summary": "Uçak siparişi", "relatedStocks": "THYAO", "publishDate": "25.09.26 10:15:00"},
        {"disclosureIndex": 102, "kapTitle": "X", "subject": "Genel Kurul Toplantı İlanı", "relatedStocks": "THYAO"},
        {"disclosureIndex": 103, "kapTitle": "Y", "subject": "Özel Durum Açıklaması (Genel)", "relatedStocks": "ABCDE"},
    ]

    def _session(self):
        s = mock.Mock()
        s.post.return_value = mock.Mock(status_code=200, json=mock.Mock(return_value=self.ITEMS))
        return s

    def test_first_run_marks_seen_without_sending(self):
        seen, send = {}, mock.Mock()
        now = datetime(2026, 9, 25, 10, 20, tzinfo=timezone.utc)
        self.assertEqual(kap.check({"THYAO"}, seen, now, send, self._session(), first_run=True), 0)
        send.assert_not_called()
        self.assertEqual(len(seen), 3)

    def test_sends_only_important_watchlist(self):
        seen, send = {}, mock.Mock()
        now = datetime(2026, 9, 25, 10, 20, tzinfo=timezone.utc)
        n = kap.check({"THYAO"}, seen, now, send, self._session())
        self.assertEqual(n, 1)                                  # genel kurul ve listede olmayan şirket gitmez
        msg = send.call_args[0][0]
        self.assertIn("📢 KAP · $THYAO", msg)
        self.assertIn("https://www.kap.org.tr/tr/Bildirim/101", msg)
        self.assertEqual(kap.check({"THYAO"}, seen, now, send, self._session()), 0)   # tekrar yok

    def test_hourly_limit_is_real_and_overflow_not_lost(self):
        now = datetime(2026, 9, 25, 10, 20, tzinfo=timezone.utc)
        log = [(now - timedelta(minutes=m)).isoformat() for m in range(10)]       # son 1 saatte 10 mesaj
        seen, send = {}, mock.Mock()
        self.assertEqual(kap.check({"THYAO"}, seen, now, send, self._session(), sent_log=log), 0)
        self.assertNotIn("101", seen)                          # sınır doldu → kaybolmadı, sonra gidecek
        later = now + timedelta(minutes=61)
        self.assertEqual(kap.check({"THYAO"}, seen, later, send, self._session(), sent_log=log), 1)

    def test_fetch_window_covers_holiday_gap(self):
        s = self._session()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        kap.check({"THYAO"}, {}, now, mock.Mock(), s, since="2026-05-26")          # bayram öncesi son kontrol
        self.assertEqual(s.post.call_args.kwargs["json"]["fromDate"], "2026-05-26")
        kap.check({"THYAO"}, {}, now, mock.Mock(), s, since="2026-01-01")          # en fazla 7 gün geri
        self.assertEqual(s.post.call_args.kwargs["json"]["fromDate"], "2026-05-25")

    def test_old_seen_pruned_by_date(self):
        now = datetime(2026, 9, 25, 10, 20, tzinfo=timezone.utc)
        seen = {"5": "2026-09-10", "101": "2026-09-25"}                      # 9 günden eski silinir
        kap.check({"THYAO"}, seen, now, mock.Mock(), self._session())
        self.assertNotIn("5", seen)
        self.assertIn("101", seen)


class TestTakvim(unittest.TestCase):
    def test_collect_and_message(self):
        from datetime import date
        today = date(2026, 9, 25)
        cals = {"AAPL": {"Earnings Date": [date(2026, 9, 26)]},
                "THYAO.IS": {"Ex-Dividend Date": date(2026, 9, 25)},
                "MSFT": {"Earnings Date": [date(2026, 10, 30)]}}
        ev = takvim.collect({"AAPL": "$AAPL", "THYAO.IS": "$THYAO", "MSFT": "$MSFT"}, today,
                            calendar_fn=lambda s: cals.get(s))
        self.assertEqual([e[2] for e in ev], ["$THYAO", "$AAPL"])
        msg = takvim.message(ev, today)
        self.assertIn("• $AAPL — yarın", msg)
        self.assertIn("• $THYAO — bugün", msg)
        self.assertIsNone(takvim.message([], today))

    def test_due(self):
        tr = ZoneInfo("Europe/Istanbul")
        self.assertTrue(takvim.due(datetime(2026, 9, 25, 9, 5, tzinfo=tr), None))
        self.assertFalse(takvim.due(datetime(2026, 9, 25, 9, 5, tzinfo=tr), "2026-09-25"))
        self.assertFalse(takvim.due(datetime(2026, 9, 25, 8, 0, tzinfo=tr), None))
        self.assertFalse(takvim.due(datetime(2026, 9, 26, 10, 0, tzinfo=tr), None))       # cumartesi


class TestKomutlar(unittest.TestCase):
    def upd(self, uid, text, chat="-100"):
        return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}

    def test_parse_and_resolve(self):
        self.assertEqual(komutlar.parse_command(self.upd(1, "/fiyat thyao"), "-100"), ("fiyat", ["thyao"]))
        self.assertEqual(komutlar.parse_command(self.upd(1, "/Durum@KRS_bot"), "-100"), ("durum", []))
        self.assertIsNone(komutlar.parse_command(self.upd(1, "/durum", chat="-999"), "-100"))   # başka sohbet
        self.assertIsNone(komutlar.parse_command(self.upd(1, "merhaba"), "-100"))
        crypto = {"BTC-USD": "BITCOIN ($)"}
        self.assertEqual(komutlar.resolve_symbol("thyao", {"THYAO"}, crypto), ("THYAO.IS", "$THYAO"))
        self.assertEqual(komutlar.resolve_symbol("btc", {"THYAO"}, crypto), ("BTC-USD", "BITCOIN ($)"))
        self.assertEqual(komutlar.resolve_symbol("$aapl", {"THYAO"}, crypto), ("AAPL", "$AAPL"))

    def test_price_and_handle(self):
        df = pd.DataFrame({"Close": [100.0, 105.0]})
        self.assertEqual(komutlar.price_text("AAPL", "$AAPL", fetch=lambda s: df), "💲 $AAPL: 105.00 (+5.00% günlük)")
        self.assertIn("gecikmeli", komutlar.price_text("THYAO.IS", "$THYAO", fetch=lambda s: df))
        send = mock.Mock()
        n = komutlar.handle([self.upd(5, "/yardim"), self.upd(6, "/bilinmeyen")], "-100",
                            {"yardim": lambda a: "liste"}, send)
        self.assertEqual(n, 1)
        self.assertEqual(komutlar.next_offset([self.upd(5, "x"), self.upd(9, "y")], 0), 10)

    def test_fiyat_exact_match(self):
        crypto = {"ETH-USD": "ETHEREUM ($)", "AVAX-USD": "AVALANCHE ($)"}
        self.assertEqual(komutlar.resolve_symbol("et", set(), crypto), ("ET", "$ET"))      # Ethereum değil
        self.assertEqual(komutlar.resolve_symbol("a", set(), crypto), ("A", "$A"))
        self.assertEqual(komutlar.resolve_symbol("ethereum", set(), crypto), ("ETH-USD", "ETHEREUM ($)"))

    def test_calendar_runs_in_background_and_caches(self):
        import threading
        gate = threading.Event()
        job = live_alerts.CalendarJob(build=lambda now: (gate.wait(2), "📅 TAKVİM")[1])
        tr = ZoneInfo("Europe/Istanbul")
        now = datetime(2026, 9, 25, 9, 5, tzinfo=tr)
        extra, send = {"tg_offset": 0}, mock.Mock()
        with mock.patch.dict(os.environ, {"TG_COMMANDS": "0", "KAP_ALERTS": "0"}):
            live_alerts.side_tasks(now, ["kripto"], {}, extra, send=send, job=job)
            self.assertTrue(job.running())                        # döngü beklemedi
            send.assert_not_called()
            gate.set()
            job.thread.join(2)
            live_alerts.side_tasks(now, ["kripto"], {}, extra, send=send, job=job)
        send.assert_called_once_with("📅 TAKVİM")
        self.assertEqual(extra["takvim_date"], "2026-09-25")
        self.assertEqual(extra["takvim_cache"]["msg"], "📅 TAKVİM")
        handlers = live_alerts.command_handlers(now, ["kripto"], {}, extra)
        self.assertEqual(handlers["takvim"]([]), "📅 TAKVİM")   # komut önbellekten, anında

    def test_side_tasks_commands_and_first_run(self):
        now = datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)          # cumartesi gece: KAP/takvim yok
        send = mock.Mock()
        extra = {}
        ups = [self.upd(7, "/durum")]
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "-100"}), \
                mock.patch.object(komutlar, "get_updates", return_value=ups):
            live_alerts.side_tasks(now, ["kripto"], {}, extra, send=send)
            send.assert_not_called()                                      # ilk açılış: eski komuta cevap yok
            self.assertEqual(extra["tg_offset"], 8)
            live_alerts.side_tasks(now, ["kripto"], {}, extra, send=send)
        self.assertIn("✅ Bot çalışıyor", send.call_args[0][0])
        self.assertIn("kripto", send.call_args[0][0])



def daily_bars(n=420, drift=0.003, seed=3, end="2026-09-25"):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + drift + 0.01 * rng.standard_normal(n))
    idx = pd.bdate_range(end=end, periods=n)
    df = pd.DataFrame({"Close": close}, index=idx)
    df["Open"] = df["Close"].shift(1).fillna(df["Close"])
    df["High"] = df[["Open", "Close"]].max(axis=1) * 1.01
    df["Low"] = df[["Open", "Close"]].min(axis=1) * 0.99
    df["Volume"] = 5_000_000.0
    return df


class TestBistPriority(unittest.TestCase):
    def test_bist_watchlist_is_bist100_bist30_first(self):
        from bot import universe as u
        wl = live_alerts.watchlist("bist", {})
        self.assertEqual({k.removesuffix(".IS") for k in wl}, set(u.BIST100))
        keys = list(wl)
        self.assertTrue(all(k.removesuffix(".IS") in u.BIST30 for k in keys[:30]))   # önce BIST 30
        b30 = u.BIST30[0]
        only100 = next(s for s in u.BIST100 if s not in u.BIST30)
        self.assertEqual(wl[f"{b30}.IS"], f"${b30} (BIST 30)")
        self.assertEqual(wl[f"{only100}.IS"], f"${only100} (BIST 100)")
        self.assertIn("AAPL", live_alerts.watchlist("us", {}))                          # ABD kapanmadı
        self.assertIn("BTC-USD", live_alerts.watchlist("kripto", {}))                   # kripto kapanmadı

    def test_kap_and_calendar_keep_old_gyos(self):
        from bot import universe as u
        syms, _ = live_alerts.calendar_symbols()
        bist = {k.removesuffix(".IS") for k in syms if k.endswith(".IS")}
        self.assertTrue(set(u.BIST100) <= bist)
        self.assertTrue({"ISGYO", "OZKGY", "KZBGY", "AKFGY"} <= bist)

    def test_cooldown_reserve(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        cd = live.Cooldown(45, 0.05, 15)
        for i in range(10):
            cd.mark(f"S{i}", 1.0, now)
        self.assertFalse(cd.allow("AAPL", 1.0, now, reserve=5))        # ABD: kalan 5 hak BIST'e ayrılmış
        self.assertTrue(cd.allow("THYAO.IS", 1.0, now, reserve=0))     # BIST kullanabilir
        self.assertTrue(cd.allow("AAPL", 1.0, now))                    # BIST seansı kapalıyken ayrım yok

    def test_bist_scanned_first_and_reserve_only_in_session(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)       # 15:00 TR: BIST açık
        fetch = mock.Mock(return_value={})
        wl = lambda m, movers: {"bist": {"THYAO.IS": "$THYAO"}, "us": {"AAPL": "$AAPL"}}.get(m, {})  # noqa: E731
        with mock.patch.object(live_alerts, "watchlist", side_effect=wl), \
                mock.patch.object(live_alerts, "market_open", return_value=True):
            live_alerts.run_once(["us", "kripto", "bist"], live.Cooldown(45, 0.05, 15), {}, now,
                                 fetch=fetch, send=mock.Mock())
        self.assertEqual(fetch.call_args_list[0][0][0], ["THYAO.IS"])


class TestAnaliz(unittest.TestCase):
    def test_resolve(self):
        from bot import universe as u
        b30 = u.BIST30[0]
        self.assertEqual(live_alerts.resolve_for_analysis(b30.lower()), (f"{b30}.IS", f"${b30} (BIST 30)", "bist"))
        self.assertEqual(live_alerts.resolve_for_analysis("aapl"), ("AAPL", "$AAPL", "hisse"))
        self.assertEqual(live_alerts.resolve_for_analysis("btc")[2], "kripto")
        self.assertEqual(live_alerts.resolve_for_analysis("MTEN"), ("MTEN.IS", "$MTEN", "bist"))   # BIST 100 dışı
        self.assertEqual(live_alerts.resolve_for_analysis("isgyo.is")[2], "gyo")

    def test_threshold_weight(self):
        from bot import universe as u
        b30 = u.BIST30[0]
        only100 = next(s for s in u.BIST_STOCKS if s not in u.BIST30)
        self.assertEqual(komutlar.analiz_esigi(f"{b30}.IS", "bist"), 70)
        self.assertEqual(komutlar.analiz_esigi(f"{only100}.IS", "bist"), 72)
        self.assertEqual(komutlar.analiz_esigi("ISGYO.IS", "gyo"), 80)
        self.assertEqual(komutlar.analiz_esigi("AAPL", "hisse"), 75)

    def test_message_in_bot_format(self):
        df = daily_bars()
        msg = komutlar.analiz_text("THYAO.IS", "$THYAO (BIST 30)", "bist", fetch=lambda s: {s: df})
        lines = msg.splitlines()
        # kanal formatı: $KOD · boş · giriş · boş · kademeler · boş · stop  (+ imza)
        self.assertEqual(lines[0], "$THYAO")
        self.assertRegex(lines[2], r"^[\d.]+-[\d.]+ giriş sağlayacağım$")
        self.assertRegex(lines[4], r"^Kademelerim:[\d.]+-[\d.]+-[\d.]+-[\d.]+-[\d.]+\+\+$")
        self.assertRegex(lines[6], r"^Stopum:[\d.]+ altı$")
        self.assertEqual(lines[-1], "(Furkanla Katla)")
        self.assertLessEqual(len(lines), 11)
        down = komutlar.analiz_text("XYZ.IS", "$XYZ", "bist", fetch=lambda s: {s: daily_bars(drift=-0.003)})
        self.assertIn("⚠️ Zayıf: bot bu hisseye sinyal vermezdi", down)
        self.assertIsNone(komutlar.analiz_text("YOK.IS", "$YOK", "bist", fetch=lambda s: {}))
        short = komutlar.analiz_text("NEW.IS", "$NEW", "bist", fetch=lambda s: {s: daily_bars(n=40)})
        self.assertIn("yeterli geçmiş yok", short)

    def test_handler_falls_back_to_us(self):
        now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        calls = []

        def fake(sym, disp, cls, fetch=None):
            calls.append(sym)
            return None if sym.endswith(".IS") else f"OK {sym}"
        with mock.patch.object(komutlar, "analiz_text", side_effect=fake):
            h = live_alerts.command_handlers(now, ["us"], {}, {})
            self.assertEqual(h["analiz"](["SMCI"]), "OK SMCI")
            self.assertEqual(calls, ["SMCI.IS", "SMCI"])
            self.assertIn("Kullanım", h["analiz"]([]))
        self.assertEqual(komutlar.parse_command(
            {"update_id": 1, "message": {"chat": {"id": -100}, "text": "/seviye thyao"}}, "-100"), ("analiz", ["thyao"]))


KAP_PAGE = (
    "<html><body><h1>MAVİ GİYİM SANAYİ VE TİCARET A.Ş.</h1><div>Geri Alınan Paylara İlişkin Bildirim</div>"
    "<table><tr><td>İşleme Konu Pay</td><td>İşlem Tarihi</td><td>İşleme Konu Payların Nominal Tutarı (TL)</td>"
    "<td>Sermayeye Oranı (%)</td><td>İşlem Fiyatı (TL/Adet)</td><td>Varsa Bu Paylara Bağlı İmtiyazlar</td></tr>"
    "<tr><td>B Grubu, MAVI, TREMAVI00037</td><td>25.09.2026</td><td>200.000</td><td>0,02517</td>"
    "<td>37,41</td><td></td></tr></table>"
    "<div>Yönetim Kurulu Karar Tarihi 18.09.2026 · ISIN TREMAVI00037</div></body></html>")


class TestGeriAlim(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(geri_alim.tr_num("4.100.050"), 4100050)
        self.assertEqual(geri_alim.tr_num("37,41"), 37.41)
        self.assertEqual(geri_alim.tr_num("200.000,00"), 200000.0)
        self.assertEqual(geri_alim.tr_num("0,02517"), 0.02517)
        self.assertEqual(geri_alim.tr_num("37.41"), 37.41)

    def test_parse_kap_table(self):
        rows = geri_alim.parse_rows(geri_alim.page_text(KAP_PAGE))
        self.assertEqual(rows, [{"tarih": "2026-09-25", "lot": 200000.0, "fiyat": 37.41, "yaklasik": False}])

    def test_parse_range_multi_rows_and_column_order(self):
        text = ("İşleme Konu Pay İşlem Tarihi Nominal Tutar Sermayeye Oranı İşlem Fiyatı "
                "DAGI, TREDAGI00018 25.09.2026 4.100.050 0,4 5,31-5,68 "
                "DAGI, TREDAGI00018 24.09.2026 1.000.000 0,1 5,20")
        rows = geri_alim.parse_rows(text)
        self.assertEqual([r["lot"] for r in rows], [4100050, 1000000])
        self.assertAlmostEqual(rows[0]["fiyat"], 5.495)
        self.assertTrue(rows[0]["yaklasik"])
        alt = "Pay Tarih Nominal İşlem Fiyatı Sermayeye Oranı X, TRAXXXX00011 25.09.2026 10.000 12,50 0,01"
        self.assertEqual(geri_alim.parse_rows(alt)[0]["fiyat"], 12.5)
        self.assertEqual(geri_alim.parse_rows("ISIN TREMAVI00037 açıklama metni"), [])

    def test_collect_and_message(self):
        from bot import universe as u
        b30 = u.BIST30[0]
        now = datetime(2026, 9, 25, 21, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
        items = [
            {"disclosureIndex": 11, "subject": "Geri Alınan Paylara İlişkin Bildirim", "relatedStocks": b30,
             "kapTitle": "BIST30 A.Ş."},
            {"disclosureIndex": 12, "subject": "Geri Alınan Paylara İlişkin Bildirim", "relatedStocks": "DAGI"},
            {"disclosureIndex": 13, "subject": "Özel Durum Açıklaması (Genel)", "summary": "Pay geri alım programı",
             "relatedStocks": "XYZ"},
            {"disclosureIndex": 14, "subject": "Geri Alınan Paylara İlişkin Bildirim", "relatedStocks": "OKUNMAZ"},
            {"disclosureIndex": 15, "subject": "Finansal Rapor", "relatedStocks": "ABC"},
            {"disclosureIndex": 10, "subject": "Geri Alınan Paylara İlişkin Bildirim", "relatedStocks": "ESKI"},
        ]
        pages = {"11": f"{b30}, TRE{b30[:4]}00011 25.09.2026 365.000 0,01 62,10",
                 "12": "DAGI, TREDAGI00018 25.09.2026 4.100.050 0,4 5,31-5,68",
                 "13": "Program duyurusu, tablo yok", "14": ""}
        seen = {"10": "2026-09-24"}
        entries = geri_alim.collect(now, seen, list_fn=lambda n, since=None: items,
                                    page_fn=lambda i: pages.get(i, ""), pause=0)
        self.assertEqual([e["no"] for e in entries], ["11", "12", "14"])     # program duyurusu ve eski yok
        self.assertEqual(set(seen), {"10", "11", "12", "13", "14"})
        self.assertAlmostEqual(entries[0]["tutar"], 365000 * 62.10)
        msg = geri_alim.message(entries, "2026-09-25")
        lines = msg.splitlines()
        self.assertEqual(lines[0], "🔁 GERİ ALIMLAR — 25.09")
        self.assertEqual(lines[2], f"${b30} ⭐BIST 30 · 365 bin lot · 22,7 mn TL")   # BIST 30 önce
        self.assertEqual(lines[3], "$DAGI · 4,1 mn lot · ≈22,5 mn TL")
        self.assertIn("Tutar okunamadı: OKUNMAZ", msg)
        self.assertIn("Toplam ≈ 45,2 mn TL", msg)
        self.assertLessEqual(len(lines), 10)
        self.assertIsNone(geri_alim.message([], "2026-09-25"))

    def test_daily_schedule_background(self):
        tr = ZoneInfo("Europe/Istanbul")
        job = live_alerts.CalendarJob(label="Geri alım özeti")
        extra, send = {}, mock.Mock()
        entry = {"no": "11", "kodlar": ["DAGI"], "sirket": "", "lot": 10.0, "tutar": 50.0, "ort": 5.0,
                 "yaklasik": False, "tarihler": ["2026-09-25"], "okundu": True}

        def fake_collect(now_tr, seen, since=None):
            seen["11"] = "2026-09-25"
            return [entry]
        with mock.patch.dict(os.environ, {"GERI_ALIM": "1"}), \
                mock.patch.object(geri_alim, "collect", side_effect=fake_collect):
            early = datetime(2026, 9, 25, 20, 0, tzinfo=tr)
            live_alerts.buyback_tasks(early, early, "2026-09-25", extra, send, 0.0, job=job)
            self.assertFalse(job.running() or job.result)                  # 21:00'dan önce başlamaz
            now = datetime(2026, 9, 25, 21, 5, tzinfo=tr)
            live_alerts.buyback_tasks(now, now, "2026-09-25", extra, send, 0.0, job=job)
            job.thread.join(2)
            live_alerts.buyback_tasks(now, now, "2026-09-25", extra, send, 0.0, job=job)
            live_alerts.buyback_tasks(now, now, "2026-09-25", extra, send, 0.0, job=job)
        send.assert_called_once()
        self.assertIn("GERİ ALIMLAR", send.call_args[0][0])
        self.assertEqual(extra["geri_alim_date"], "2026-09-25")
        self.assertEqual(extra["geri_alim_seen"], {"11": "2026-09-25"})
        h = live_alerts.command_handlers(now, ["bist"], {}, extra)
        self.assertIn("GERİ ALIMLAR", h["geri"]([]))
        self.assertIn("21:00", live_alerts.command_handlers(now, ["bist"], {}, {})["geri"]([]))
        sat = datetime(2026, 9, 26, 21, 30, tzinfo=tr)                      # cumartesi: çalışmaz
        live_alerts.buyback_tasks(sat, sat, "2026-09-26", {}, send, 0.0, job=job)
        self.assertFalse(job.running())


if __name__ == "__main__":
    unittest.main()
