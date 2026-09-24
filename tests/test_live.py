"""Canlı alarm testleri (1 dakikalık sahte veriyle, internet gerektirmez)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DRY_RUN"] = "1"
os.environ.pop("GEMINI_API_KEY", None)

from bot import filtre, live  # noqa: E402
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
        self.assertTrue(msg.startswith("⚡ $XYZ ANİ HAREKET"))
        for part in ("giriş bölgesi", "Kademeler:", "Stop:", "📰 Haber: Şirket", "(Furkanla Katla)"):
            self.assertIn(part, msg)
        a.delayed = True
        self.assertIn("15 dk gecikmeli", live.alert_message(a))


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
        self.assertTrue(msg.startswith("⚡ $XYZ ANİ HAREKET"))
        self.assertIn("Kademeler: 11.00 - 12.00 - 13.00", msg)
        self.assertNotIn("giriş bölgesi", msg)                  # eski satırlar yok
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
        self.assertIn("🚪 $XYZ ÇIKIŞ: Stop çalıştı", msg)
        self.assertIn("%-10.0", msg)

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
        self.assertIn("ÇIKIŞ: Mum VWAP altında", msg)
        old = filtre.OpenSignal("A", "$A", "us", 1, 0.9, 1.1, 1, "2026-09-24T00:00:00+00:00")
        self.assertTrue(filtre.expired(old, datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc)))

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
        self.assertIn("ÇIKIŞ", send.call_args[0][0])

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


if __name__ == "__main__":
    unittest.main()
