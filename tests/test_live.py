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

from bot import live  # noqa: E402
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
        with mock.patch.object(live_alerts, "watchlist", return_value={"XYZ": "$XYZ", "AAPL": "$AAPL"}):
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


if __name__ == "__main__":
    unittest.main()
