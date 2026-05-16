"""Tests for KalshiTradingClient."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.models import Order
from kalshi_bot.client import KalshiTradingClient

import datetime


@pytest.fixture
def client(rsa_private_key, tmp_path, key_id):
    """KalshiTradingClient with a real in-memory key (no file needed)."""
    # Write key to tmp file so load_private_key can read it
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption
    )
    key_path = tmp_path / "test_key.pem"
    key_path.write_bytes(
        rsa_private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    return KalshiTradingClient(key_id=key_id, private_key_path=str(key_path), demo=True)


def _mock_response(data: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _order(symbol="KXINX-24", side="buy", qty=10, price=0.55):
    return Order(
        symbol=symbol,
        side=side,
        quantity=Decimal(str(qty)),
        limit_price=Decimal(str(price)),
        created_at=datetime.datetime.now(tz=datetime.timezone.utc),
    )


class TestPlaceLimitOrder:
    def test_buy_order_returns_filled(self, client):
        resp_data = {
            "order": {
                "status":                  "executed",
                "fill_count_fp":           "10.00",
                "taker_fill_cost_dollars": "0.5500",
                "maker_fill_cost_dollars": "0.0000",
                "taker_fees_dollars":      "0.0346",
                "maker_fees_dollars":      "0.0000",
            }
        }
        with patch("requests.request", return_value=_mock_response(resp_data)):
            result = client.place_limit_order(_order())
        assert result.status == "filled"
        assert result.filled_qty == Decimal("10")
        assert result.avg_fill_price == Decimal("0.055000")

    def test_resting_order_returns_partial(self, client):
        resp_data = {"order": {"status": "resting", "fill_count_fp": "0.00"}}
        with patch("requests.request", return_value=_mock_response(resp_data)):
            result = client.place_limit_order(_order())
        assert result.status == "partial"

    def test_price_converted_to_cents(self, client):
        """limit_price 0.65 should be sent to Kalshi as yes_price=65."""
        captured = {}
        def fake_request(method, url, headers, json=None, timeout=10):
            captured["body"] = json
            return _mock_response({"order": {"status": "filled", "filled_count": 5, "yes_price": 65}})

        with patch("requests.request", side_effect=fake_request):
            client.place_limit_order(_order(price=0.65, qty=5))

        assert captured["body"]["yes_price"] == 65

    def test_api_error_raises(self, client):
        from trading_bot.exceptions import TradingClientError
        with patch("requests.request", return_value=_mock_response({}, status=400)):
            with pytest.raises(TradingClientError):
                client.place_limit_order(_order())


class TestGetBalance:
    def test_converts_cents_to_dollars(self, client):
        with patch("requests.request", return_value=_mock_response({"balance": 150000})):
            balance = client.get_balance()
        assert balance == Decimal("1500.0")

    def test_zero_balance(self, client):
        with patch("requests.request", return_value=_mock_response({"balance": 0})):
            assert client.get_balance() == Decimal("0.0")


class TestGetPositions:
    def test_empty_positions(self, client):
        with patch("requests.request", return_value=_mock_response({"market_positions": []})):
            df = client.get_positions()
        assert df.empty
        assert "symbol" in df.columns
        assert "quantity" in df.columns
        assert "avg_entry_price" in df.columns

    def test_positions_returned_as_decimal(self, client):
        data = {
            "market_positions": [
                {"ticker": "KXINX-24", "position_fp": "10", "market_exposure_dollars": 5.50}
            ]
        }
        with patch("requests.request", return_value=_mock_response(data)):
            df = client.get_positions()

        assert len(df) == 1
        row = df.iloc[0]
        assert row["symbol"] == "KXINX-24"
        assert isinstance(row["quantity"], Decimal)
        assert isinstance(row["avg_entry_price"], Decimal)
        assert row["quantity"] == Decimal("10")
        # avg_entry_price = market_exposure_dollars / position_fp = 5.50 / 10 = 0.55
        assert row["avg_entry_price"] == Decimal("0.55")

    def test_zero_position_excluded(self, client):
        data = {
            "market_positions": [
                {"ticker": "KXINX-24", "position_fp": "0", "market_exposure_dollars": 0}
            ]
        }
        with patch("requests.request", return_value=_mock_response(data)):
            df = client.get_positions()
        assert df.empty


class TestCancelOrder:
    def test_returns_true_on_success(self, client):
        with patch("requests.request", return_value=_mock_response({})):
            assert client.cancel_order("order-123") is True

    def test_returns_false_on_error(self, client):
        with patch("requests.request", return_value=_mock_response({}, status=404)):
            assert client.cancel_order("order-123") is False
