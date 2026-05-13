"""Tests for kalshi_bot sell engine extensions."""

from __future__ import annotations

import pandas as pd
import pytest

from kalshi_bot.sell_engine import CompositeSellEngine, StopLossTakeProfitSellEngine


def _positions(*rows) -> pd.DataFrame:
    """Build a positions DataFrame from (symbol, avg_entry_price) tuples."""
    return pd.DataFrame(
        [{"symbol": s, "avg_entry_price": p, "quantity": 10} for s, p in rows]
    )


def _prices(**symbol_bid) -> pd.DataFrame:
    """Build a prices DataFrame from keyword args symbol=bid (yes_bid is the exit price)."""
    return pd.DataFrame(
        [{"symbol": s, "yes_bid": b, "yes_ask": b} for s, b in symbol_bid.items()]
    )


class TestStopLossTakeProfitSellEngine:
    def _engine(self, tp=0.10, sl=0.10):
        return StopLossTakeProfitSellEngine(take_profit=tp, stop_loss=sl)

    def test_no_sell_within_band(self):
        eng = self._engine()
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.42}),   # +5% — inside band
        )
        assert result.empty

    def test_take_profit_triggered(self):
        eng = self._engine(tp=0.10)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.46}),   # +15% — clearly above 10% TP
        )
        assert "KXINX-A" in result["symbol"].values

    def test_stop_loss_triggered(self):
        eng = self._engine(sl=0.10)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.34}),   # -15% — clearly below -10% SL
        )
        assert "KXINX-A" in result["symbol"].values

    def test_only_breached_position_sold(self):
        eng = self._engine(tp=0.10, sl=0.10)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40), ("KXINX-B", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.46, "KXINX-B": 0.41}),  # A hits TP, B stays
        )
        assert list(result["symbol"]) == ["KXINX-A"]

    def test_zero_price_skipped(self):
        eng = self._engine(sl=0.10)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.0}),
        )
        assert result.empty

    def test_empty_positions_returns_empty(self):
        eng = self._engine()
        result = eng.evaluate(pd.DataFrame(), [], _prices(**{"KXINX-A": 0.44}))
        assert result.empty

    def test_symbol_not_in_prices_skipped(self):
        eng = self._engine(tp=0.10)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-B": 0.50}),   # different symbol
        )
        assert result.empty


class TestCompositeSellEngine:
    def test_sells_if_either_engine_triggers(self):
        always_sell = StopLossTakeProfitSellEngine(take_profit=0.01, stop_loss=0.01)
        never_sell  = StopLossTakeProfitSellEngine(take_profit=99.0, stop_loss=99.0)
        eng = CompositeSellEngine(always_sell, never_sell)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.42}),
        )
        assert "KXINX-A" in result["symbol"].values

    def test_no_sell_when_no_engine_triggers(self):
        never1 = StopLossTakeProfitSellEngine(take_profit=99.0, stop_loss=99.0)
        never2 = StopLossTakeProfitSellEngine(take_profit=99.0, stop_loss=99.0)
        eng = CompositeSellEngine(never1, never2)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.42}),
        )
        assert result.empty

    def test_deduplicates_positions(self):
        # Both engines trigger on the same symbol — result should have one row
        tp_eng = StopLossTakeProfitSellEngine(take_profit=0.01)
        sl_eng = StopLossTakeProfitSellEngine(stop_loss=0.01)
        eng    = CompositeSellEngine(tp_eng, sl_eng)
        result = eng.evaluate(
            _positions(("KXINX-A", 0.40)),
            [],
            _prices(**{"KXINX-A": 0.42}),
        )
        assert len(result) == 1
