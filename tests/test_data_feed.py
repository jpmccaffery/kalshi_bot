"""Tests for KalshiDataFeed."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import json

import pytest

from kalshi_bot.data_feed import KalshiDataFeed, _to_float, resolve_symbols



@pytest.fixture
def feed(rsa_private_key, tmp_path, key_id):
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption
    )
    key_path = tmp_path / "test_key.pem"
    key_path.write_bytes(
        rsa_private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    return KalshiDataFeed(
        symbols=["KXINX-24", "KXETHD-24"],
        key_id=key_id,
        private_key_path=str(key_path),
        lookback_bars=5,
        demo=True,
    )


def _market_obj(ticker, yes_bid="0.45", yes_ask="0.47", no_bid="0.53", no_ask="0.55",
                volume=1000, open_interest=500):
    """A single market dict as returned inside the bulk markets response."""
    return {
        "ticker":           ticker,
        "yes_bid_dollars":  yes_bid,
        "yes_ask_dollars":  yes_ask,
        "no_bid_dollars":   no_bid,
        "no_ask_dollars":   no_ask,
        "volume_fp":        volume,
        "open_interest_fp": open_interest,
    }


def _bulk_response(*market_objs, cursor=None):
    """Wrap market dicts in the bulk endpoint envelope."""
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"markets": list(market_objs), "cursor": cursor}
    return m


class TestToFloat:
    def test_string_decimal(self):
        assert _to_float("0.50") == pytest.approx(0.50)
        assert _to_float("0.01") == pytest.approx(0.01)
        assert _to_float("1.00") == pytest.approx(1.00)

    def test_numeric(self):
        assert _to_float(0.47) == pytest.approx(0.47)

    def test_none_returns_nan(self):
        import math
        assert math.isnan(_to_float(None))

    def test_none_returns_default(self):
        assert _to_float(None, default=0.0) == 0.0


class TestProvidedSchema:
    def test_schema_has_expected_columns(self, feed):
        schema = feed.provided_schema
        for col in ("symbol", "ts", "yes_bid", "yes_ask", "no_bid", "no_ask",
                    "volume", "open_interest"):
            assert col in schema.columns


class TestFetch:
    # feed symbols: ["KXINX-24", "KXETHD-24"]
    # event groups: KXINX → ["KXINX-24"],  KXETHD → ["KXETHD-24"]
    # → 2 bulk requests per tick

    def _empty_positions(self):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"market_positions": []}
        return m

    def _tick_mocks(self, yes_ask_kxinx="0.47", yes_ask_kxethd="0.32"):
        # positions pre-fetch + one bulk response per event (KXINX, KXETHD)
        return [
            self._empty_positions(),
            _bulk_response(_market_obj("KXINX-24",  yes_ask=yes_ask_kxinx)),
            _bulk_response(_market_obj("KXETHD-24", yes_ask=yes_ask_kxethd)),
        ]

    def test_bars_has_one_row_per_symbol(self, feed):
        with patch("requests.get", side_effect=self._tick_mocks()):
            from trading_bot.models import Tick
            import datetime
            tick = Tick(ts=datetime.datetime.now(tz=datetime.timezone.utc),
                        index=0, is_final=False)
            snapshot = feed.fetch(tick)

        assert len(snapshot.bars) == 2
        assert set(snapshot.bars["symbol"].tolist()) == {"KXINX-24", "KXETHD-24"}

    def test_prices_normalised_to_probability(self, feed):
        with patch("requests.get", side_effect=self._tick_mocks(yes_ask_kxinx="0.47")):
            from trading_bot.models import Tick
            import datetime
            tick = Tick(ts=datetime.datetime.now(tz=datetime.timezone.utc),
                        index=0, is_final=False)
            snapshot = feed.fetch(tick)

        row = snapshot.bars[snapshot.bars["symbol"] == "KXINX-24"].iloc[0]
        assert row["yes_ask"] == pytest.approx(0.47)

    def test_history_accumulates_across_ticks(self, feed):
        from trading_bot.models import Tick
        import datetime

        def make_tick(i):
            return Tick(
                ts=datetime.datetime(2024, 1, i + 1, tzinfo=datetime.timezone.utc),
                index=i, is_final=False,
            )

        for i in range(3):
            with patch("requests.get", side_effect=self._tick_mocks()):
                snapshot = feed.fetch(make_tick(i))

        # After 3 ticks, each symbol should have 3 history rows
        for sym in ["KXINX-24", "KXETHD-24"]:
            sym_hist = snapshot.history[snapshot.history["symbol"] == sym]
            assert len(sym_hist) == 3


def _mock_page(key: str, items: list, cursor: str | None = None):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {key: items, "cursor": cursor}
    return m


class TestResolveSymbols:
    def _args(self, rsa_private_key, tmp_path, key_id):
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption,
        )
        key_path = tmp_path / "key.pem"
        key_path.write_bytes(
            rsa_private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8,
                                           NoEncryption())
        )
        return dict(key_id=key_id, private_key_path=str(key_path), demo=True)

    def test_returns_market_tickers_for_series(self, rsa_private_key, tmp_path, key_id):
        # events call returns one event; markets call returns two tickers
        events_resp  = _mock_page("events",  [{"event_ticker": "KXINX-26APR14H1600"}])
        markets_resp = _mock_page("markets", [
            {"ticker": "KXINX-26APR14H1600-B7100"},
            {"ticker": "KXINX-26APR14H1600-T7200"},
        ])
        with patch("requests.get", side_effect=[events_resp, markets_resp]):
            result = resolve_symbols(["KXINX"], **self._args(rsa_private_key, tmp_path, key_id))
        assert result == ["KXINX-26APR14H1600-B7100", "KXINX-26APR14H1600-T7200"]

    def test_multiple_series(self, rsa_private_key, tmp_path, key_id):
        responses = [
            _mock_page("events",  [{"event_ticker": "KXINX-26APR14H1600"}]),
            _mock_page("markets", [{"ticker": "KXINX-26APR14H1600-B7100"}]),
            _mock_page("events",  [{"event_ticker": "KXEURUSD-26APR14H1600"}]),
            _mock_page("markets", [{"ticker": "KXEURUSD-26APR14H1600-B1.18"}]),
        ]
        with patch("requests.get", side_effect=responses):
            result = resolve_symbols(["KXINX", "KXEURUSD"],
                                     **self._args(rsa_private_key, tmp_path, key_id))
        assert "KXINX-26APR14H1600-B7100"    in result
        assert "KXEURUSD-26APR14H1600-B1.18" in result

    def test_multiple_events_per_series(self, rsa_private_key, tmp_path, key_id):
        responses = [
            _mock_page("events", [
                {"event_ticker": "KXINX-26APR14H1500"},
                {"event_ticker": "KXINX-26APR14H1600"},
            ]),
            _mock_page("markets", [{"ticker": "KXINX-26APR14H1500-B7050"}]),
            _mock_page("markets", [{"ticker": "KXINX-26APR14H1600-B7100"}]),
        ]
        with patch("requests.get", side_effect=responses):
            result = resolve_symbols(["KXINX"], **self._args(rsa_private_key, tmp_path, key_id))
        assert len(result) == 2

    def test_no_events_returns_empty(self, rsa_private_key, tmp_path, key_id):
        events_resp = _mock_page("events", [])
        with patch("requests.get", return_value=events_resp):
            result = resolve_symbols(["KXINX"], **self._args(rsa_private_key, tmp_path, key_id))
        assert result == []

    def test_api_error_raises(self, rsa_private_key, tmp_path, key_id):
        from trading_bot.exceptions import DataFeedError
        m = MagicMock()
        m.status_code = 401
        m.text = "Unauthorized"
        with patch("requests.get", return_value=m):
            with pytest.raises(DataFeedError):
                resolve_symbols(["KXINX"], **self._args(rsa_private_key, tmp_path, key_id))

    def test_results_sorted(self, rsa_private_key, tmp_path, key_id):
        responses = [
            _mock_page("events",  [{"event_ticker": "KXINX-26APR14H1600"}]),
            _mock_page("markets", [
                {"ticker": "KXINX-26APR14H1600-T7200"},
                {"ticker": "KXINX-26APR14H1600-B7050"},
                {"ticker": "KXINX-26APR14H1600-B7100"},
            ]),
        ]
        with patch("requests.get", side_effect=responses):
            result = resolve_symbols(["KXINX"], **self._args(rsa_private_key, tmp_path, key_id))
        assert result == sorted(result)
