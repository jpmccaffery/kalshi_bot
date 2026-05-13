"""Tests for recommenders."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_bot.models import MarketSnapshot, Tick

from kalshi_bot.recommender import AtmCheapBuyerRecommender, ProbMeanReversionRecommender


def _snapshot(symbol_prices: dict[str, list[float]]) -> MarketSnapshot:
    """
    Build a MarketSnapshot from {symbol: [yes_ask_prices...]}.
    Last value in each list is the current bar.
    """
    tz   = ZoneInfo("UTC")
    base = datetime.datetime(2024, 1, 1, tzinfo=tz)

    rows = []
    for sym, prices in symbol_prices.items():
        for i, p in enumerate(prices):
            rows.append({
                "symbol":  sym,
                "ts":      base + datetime.timedelta(days=i),
                "yes_ask": p,
            })

    history = pd.DataFrame(rows)
    # Current bar = latest row per symbol
    bars = history.groupby("symbol").last().reset_index()
    now  = base + datetime.timedelta(days=len(next(iter(symbol_prices.values()))) - 1)

    return MarketSnapshot(ts=now, bars=bars, history=history, extras={})


class TestProbMeanReversionRecommender:
    def setup_method(self):
        self.rec = ProbMeanReversionRecommender(window=5, threshold=0.05)

    def test_no_signal_when_insufficient_history(self):
        # Only 3 bars, window=5 → no signal
        snap = _snapshot({"KXINX-24": [0.50, 0.48, 0.46]})
        assert self.rec.recommend(snap) == []

    def test_no_signal_when_price_above_mean(self):
        # Prices trending up — current is above rolling mean
        prices = [0.40, 0.42, 0.44, 0.46, 0.48, 0.60]   # big jump up
        snap = _snapshot({"KXINX-24": prices})
        assert self.rec.recommend(snap) == []

    def test_signal_when_price_dips_below_mean(self):
        # Mean of last 5: (0.60, 0.60, 0.60, 0.60, 0.60) = 0.60
        # Current yes_ask = 0.50 → dip = (0.60-0.50)/0.60 = 0.167 > threshold 0.05
        prices = [0.60, 0.60, 0.60, 0.60, 0.60, 0.50]
        snap = _snapshot({"KXINX-24": prices})
        signals = self.rec.recommend(snap)
        assert len(signals) == 1
        assert signals[0].symbol == "KXINX-24"
        assert signals[0].direction == "long"
        assert signals[0].edge > 0

    def test_edge_proportional_to_dip(self):
        # Larger dip → larger edge
        small_dip_prices = [0.60] * 5 + [0.54]   # dip 10%
        large_dip_prices = [0.60] * 5 + [0.45]   # dip 25%

        sigs_small = self.rec.recommend(_snapshot({"KXINX-24": small_dip_prices}))
        sigs_large = self.rec.recommend(_snapshot({"KXINX-24": large_dip_prices}))

        assert sigs_large[0].edge > sigs_small[0].edge

    def test_edge_capped_at_030(self):
        # Extreme dip should still cap at 0.30
        prices = [0.90] * 5 + [0.10]
        snap = _snapshot({"KXINX-24": prices})
        signals = self.rec.recommend(snap)
        assert signals[0].edge <= 0.30

    def test_signals_sorted_by_descending_edge(self):
        prices_high = {"KXINX-24": [0.60] * 5 + [0.40]}   # bigger dip
        prices_low  = {"KXETHD-24": [0.60] * 5 + [0.55]}  # smaller dip
        combined = {**prices_high, **prices_low}
        snap = _snapshot(combined)
        signals = self.rec.recommend(snap)
        assert signals[0].symbol == "KXINX-24"   # higher edge first

    def test_metadata_contains_expected_keys(self):
        prices = [0.60] * 5 + [0.50]
        snap = _snapshot({"KXINX-24": prices})
        sig = self.rec.recommend(snap)[0]
        assert "yes_ask" in sig.metadata
        assert "rolling_mean" in sig.metadata
        assert "dip_pct" in sig.metadata

    def test_required_schema_has_yes_ask(self):
        assert "yes_ask" in self.rec.required_schema.columns


def _atm_snapshot(ticker_asks: dict[str, float]) -> MarketSnapshot:
    """Build a minimal snapshot with one bar per ticker."""
    tz  = ZoneInfo("UTC")
    now = datetime.datetime(2024, 1, 1, tzinfo=tz)
    rows = [{"symbol": t, "ts": now, "yes_ask": a} for t, a in ticker_asks.items()]
    bars = pd.DataFrame(rows)
    return MarketSnapshot(ts=now, bars=bars, history=bars.copy(), extras={})


class TestAtmCheapBuyerRecommender:
    def setup_method(self):
        self.rec = AtmCheapBuyerRecommender(ceiling=0.48)

    def test_signals_atm_contract(self):
        # Two strikes for same series+expiry — picks the one closer to 0.50
        snap = _atm_snapshot({
            "KXINX-26APR14H1600-B7100": 0.44,   # closer to 0.50
            "KXINX-26APR14H1600-B6900": 0.10,   # far OTM
        })
        signals = self.rec.recommend(snap)
        assert len(signals) == 1
        assert signals[0].symbol == "KXINX-26APR14H1600-B7100"

    def test_no_signal_when_atm_above_ceiling(self):
        snap = _atm_snapshot({"KXINX-26APR14H1600-B7100": 0.50})
        assert self.rec.recommend(snap) == []

    def test_one_signal_per_series_expiry(self):
        # Two different expiries for the same series → two signals
        snap = _atm_snapshot({
            "KXINX-26APR14H1600-B7100": 0.44,
            "KXINX-26APR15H1600-B7050": 0.43,
        })
        signals = self.rec.recommend(snap)
        assert len(signals) == 2

    def test_two_series_same_expiry(self):
        # Different series, same date → two signals (one per series)
        snap = _atm_snapshot({
            "KXINX-26APR14H1600-B7100":    0.44,
            "KXEURUSD-26APR14H1600-B1.18": 0.43,
        })
        signals = self.rec.recommend(snap)
        assert len(signals) == 2

    def test_edge_is_ceiling_minus_ask(self):
        snap = _atm_snapshot({"KXINX-26APR14H1600-B7100": 0.40})
        sig  = self.rec.recommend(snap)[0]
        assert sig.edge == pytest.approx(0.08, abs=1e-4)

    def test_signals_sorted_by_descending_edge(self):
        snap = _atm_snapshot({
            "KXINX-26APR14H1600-B7100":    0.44,   # edge 0.04
            "KXEURUSD-26APR14H1600-B1.18": 0.40,   # edge 0.08
        })
        signals = self.rec.recommend(snap)
        assert signals[0].symbol == "KXEURUSD-26APR14H1600-B1.18"

    def test_metadata_keys(self):
        snap = _atm_snapshot({"KXINX-26APR14H1600-B7100": 0.44})
        sig  = self.rec.recommend(snap)[0]
        assert "series"  in sig.metadata
        assert "expiry"  in sig.metadata
        assert "yes_ask" in sig.metadata

    def test_skips_malformed_tickers(self):
        snap = _atm_snapshot({"BADTICKER": 0.40})
        assert self.rec.recommend(snap) == []

    def test_required_schema_has_yes_ask(self):
        assert "yes_ask" in self.rec.required_schema.columns
