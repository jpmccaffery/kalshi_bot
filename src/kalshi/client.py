"""
Kalshi REST API client with RSA-based authentication.

Auth setup:
  1. Generate a key pair:
       openssl genrsa -out kalshi_private.pem 2048
       openssl rsa -in kalshi_private.pem -pubout -out kalshi_public.pem
  2. Register the public key at https://kalshi.com/account/api
  3. Copy the Key ID shown after registration into your .env file.
"""

import base64
import logging
import time
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.kalshi.models import Fill, Market, OrderAction, OrderBook, OrderBookLevel, Side

logger = logging.getLogger(__name__)


class KalshiClient:
    def __init__(self, base_url: str, api_key_id: str, private_key_path: str):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        # Path prefix to include in the signature (e.g. "/trade-api/v2")
        from urllib.parse import urlparse
        self._path_prefix = urlparse(self.base_url).path
        self._private_key = None

        if private_key_path:
            try:
                with open(private_key_path, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(f.read(), password=None)
            except FileNotFoundError:
                logger.warning("Private key file not found: %s — auth will fail", private_key_path)

    # ------------------------------------------------------------------ auth

    def _auth_headers(self, method: str, path: str) -> dict:
        if not self._private_key:
            return {}

        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + method.upper() + self._path_prefix + path).encode()
        signature = self._private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(signature).decode()

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base_url + path
        headers = self._auth_headers("GET", path)
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = self.base_url + path
        headers = self._auth_headers("POST", path)
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        url = self.base_url + path
        headers = self._auth_headers("DELETE", path)
        resp = requests.delete(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # --------------------------------------------------------- market data

    @staticmethod
    def _dollars_to_cents(val: str, default: int) -> int:
        try:
            return round(float(val) * 100)
        except (TypeError, ValueError):
            return default

    def get_market(self, ticker: str) -> Market:
        data = self._get(f"/markets/{ticker}")
        m = data["market"]
        return Market(
            ticker=m["ticker"],
            title=m["title"],
            status=m["status"],
            yes_bid=self._dollars_to_cents(m.get("yes_bid_dollars"), 0),
            yes_ask=self._dollars_to_cents(m.get("yes_ask_dollars"), 100),
            volume=m.get("volume", 0),
            open_interest=m.get("open_interest", 0),
            close_time=m.get("close_time"),
        )

    def get_markets(self, tickers: list[str]) -> list[Market]:
        markets = []
        for ticker in tickers:
            try:
                markets.append(self.get_market(ticker))
            except Exception as e:
                logger.error("Failed to fetch market %s: %s", ticker, e)
        return markets

    def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        ob = data.get("orderbook", {})
        return OrderBook(
            ticker=ticker,
            yes_bids=[OrderBookLevel(p, q) for p, q in ob.get("yes", [])],
            yes_asks=[OrderBookLevel(p, q) for p, q in ob.get("no", [])],  # no side = yes asks
        )

    # --------------------------------------------------------- portfolio

    def get_balance_cents(self) -> int:
        data = self._get("/portfolio/balance")
        return data.get("balance", 0)

    def get_positions(self) -> dict:
        return self._get("/portfolio/positions")

    def place_order(
        self,
        ticker: str,
        side: Side,
        action: OrderAction,
        quantity: int,
        limit_price: Optional[int] = None,
    ) -> dict:
        body = {
            "ticker": ticker,
            "side": side.value,
            "action": action.value,
            "count": quantity,
            "type": "limit" if limit_price is not None else "market",
        }
        if limit_price is not None:
            body["limit_price"] = limit_price

        return self._post("/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self._delete(f"/portfolio/orders/{order_id}")
