"""
KalshiTradingClient — live trading client for the Kalshi exchange.

Implements TradingClientProtocol from the trading_bot framework.

Order mapping
-------------
framework Order.side == "buy"   → Kalshi action=buy,  side=yes
framework Order.side == "sell"  → Kalshi action=sell, side=yes

Kalshi prices are submitted as integer cents (1–99); the framework works in
decimal probabilities (0.01–0.99).  Conversion: cents = round(prob * 100).

Balances are returned by Kalshi in cents; converted to Decimal dollars here.

Positions
---------
Kalshi /portfolio/positions returns market_positions, each with:
    ticker, position_fp (net YES contracts), market_exposure_dollars (cost basis $)
    avg_entry_price = market_exposure_dollars / position_fp  (no native field)

The framework expects columns: symbol, quantity, avg_entry_price, entry_ts,
original_edge.  Quantities and prices are kept as Decimal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.exceptions import TradingClientError
from trading_bot.models import Order, OrderResult

from kalshi_bot.auth import auth_headers, load_private_key

logger = logging.getLogger(__name__)

_DEMO_BASE = "https://demo-api.kalshi.co"
_LIVE_BASE = "https://api.elections.kalshi.com"

_STATUS_MAP = {
    "executed":         "filled",   # Kalshi term for a fully-filled order
    "filled":           "filled",
    "partially_filled": "partial",
    "resting":          "partial",
    "canceled":         "rejected",
    "pending":          "partial",
}

_POSITION_COLS = ["symbol", "quantity", "avg_entry_price", "entry_ts", "original_edge"]


class KalshiTradingClient:
    """
    Live trading client for the Kalshi prediction-market exchange.

    Parameters
    ----------
    key_id:
        Kalshi API key ID.
    private_key_path:
        Path to the RSA private key PEM file.
    demo:
        If True (default), target the Kalshi demo environment.
    tz:
        Timezone for entry_ts timestamps.
    """

    def __init__(
        self,
        key_id: str,
        private_key_path: str,
        demo: bool = True,
        tz: ZoneInfo = ZoneInfo("UTC"),
    ) -> None:
        self._base_url    = _DEMO_BASE if demo else _LIVE_BASE
        self._key_id      = key_id
        self._private_key = load_private_key(private_key_path)
        self._tz          = tz
        self._entry_times:   dict[str, datetime] = {}
        self._prev_tickers:  set[str] | None     = None   # tickers held at end of last tick
        self._prev_qtys:     dict[str, Decimal]  = {}     # qty/cost for settlement calc

    # ------------------------------------------------------------------
    # TradingClientProtocol
    # ------------------------------------------------------------------

    def place_limit_order(self, order: Order) -> OrderResult:
        path = "/trade-api/v2/portfolio/orders"
        kalshi_side = order.metadata.get("kalshi_side", "yes")
        price_cents = max(1, min(99, round(float(order.limit_price) * 100)))
        price_key   = "no_price" if kalshi_side == "no" else "yes_price"
        body = {
            "ticker":          order.symbol,
            "client_order_id": order.order_id,
            "type":            "limit",
            "action":          order.side,        # "buy" or "sell"
            "side":            kalshi_side,
            "count":           max(1, round(float(order.quantity))),
            price_key:         price_cents,
            "time_in_force":   "immediate_or_cancel",
        }
        data = self._request("POST", path, json_body=body)
        if "order" not in data:
            raise TradingClientError(
                f"place_limit_order response missing 'order' key: {data}"
            )
        resp_order = data["order"]
        logger.debug("place_limit_order raw response: %s", resp_order)

        raw_status = resp_order.get("status", "")
        filled_qty = Decimal(str(resp_order.get("fill_count_fp") or 0))

        # An IOC order that partially fills comes back as "canceled" for the
        # unfilled remainder — treat it as "filled" if anything was filled,
        # otherwise map normally.
        if raw_status == "canceled" and filled_qty > 0:
            status = "filled"
        else:
            status = _STATUS_MAP.get(raw_status, "partial")

        # Actual fill price: total cost paid divided by contracts filled.
        taker_cost = Decimal(str(resp_order.get("taker_fill_cost_dollars") or 0))
        maker_cost = Decimal(str(resp_order.get("maker_fill_cost_dollars") or 0))
        total_cost = taker_cost + maker_cost
        avg_fill   = (total_cost / filled_qty).quantize(Decimal("0.000001")) if filled_qty > 0 else None

        # Actual fees reported by the exchange.
        taker_fees = Decimal(str(resp_order.get("taker_fees_dollars") or 0))
        maker_fees = Decimal(str(resp_order.get("maker_fees_dollars") or 0))
        fees_paid  = taker_fees + maker_fees

        logger.info(
            "KalshiTradingClient: %s %s (%s) qty=%s limit=%s fill=%s → %s (fees=$%.4f)",
            order.side, order.symbol, kalshi_side,
            filled_qty, order.limit_price,
            avg_fill if avg_fill else "0",
            status, float(fees_paid),
        )

        return OrderResult(
            order_id      = order.order_id,
            status        = status,
            filled_qty    = filled_qty,
            avg_fill_price= avg_fill,
            fees_paid     = fees_paid,
            exchange_ts   = datetime.now(tz=timezone.utc),
            raw_response  = resp_order,
        )

    def cancel_order(self, order_id: str) -> bool:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        try:
            self._request("DELETE", path)
            logger.debug("Cancelled order %s", order_id)
            return True
        except TradingClientError as exc:
            logger.warning("Failed to cancel order %s: %s", order_id, exc)
            return False

    def get_positions(self) -> pd.DataFrame:
        data      = self._request("GET", "/trade-api/v2/portfolio/positions")
        positions = data.get("market_positions", [])

        if not positions:
            return pd.DataFrame(columns=_POSITION_COLS)

        rows = []
        for pos in positions:
            # position_fp is net YES contracts held (fixed-point string)
            if "position_fp" not in pos:
                logger.warning(
                    "Position for %s missing 'position_fp' — skipping",
                    pos.get("ticker", "?"),
                )
                continue
            qty = float(pos["position_fp"] or 0)
            if qty <= 0:
                continue
            qty = int(qty)
            # market_exposure_dollars is the cost basis in dollars (per API docs)
            exposure  = float(pos.get("market_exposure_dollars",
                                      pos.get("market_exposure", 0)) or 0)
            avg_price = Decimal(str(exposure / qty if qty else 0))

            ticker = pos.get("ticker", "")
            if ticker not in self._entry_times:
                self._entry_times[ticker] = datetime.now(tz=self._tz)
                logger.debug("Recording entry time for new position %s", ticker)

            rows.append({
                "symbol":          ticker,
                "quantity":        Decimal(str(qty)),
                "avg_entry_price": avg_price,
                "entry_ts":        self._entry_times[ticker],
                "original_edge":   float("nan"),
            })

        # Prune entry times for positions that are no longer held
        current = {pos.get("ticker", "") for pos in positions if float(pos.get("position_fp", 0) or 0) > 0}
        self._entry_times = {k: v for k, v in self._entry_times.items() if k in current}

        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=_POSITION_COLS)

    def get_balance(self) -> Decimal:
        data    = self._request("GET", "/trade-api/v2/portfolio/balance")
        # Kalshi returns balance in cents
        balance = data.get("balance", 0)
        return Decimal(str(balance / 100.0))

    def update_prices(self, bars: pd.DataFrame) -> None:
        # No-op for a live client — prices come from the exchange in real time
        pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def detect_settlements(self, output_dir: "Path | None") -> None:
        """
        Compare current live positions against last tick's positions.
        Tickers that disappeared are queried for their settlement result
        and written to settlements.csv in output_dir.
        Called once per tick from run.py after all tick processing.
        """
        import csv as _csv
        from pathlib import Path as _Path

        data      = self._request("GET", "/trade-api/v2/portfolio/positions")
        positions = data.get("market_positions", [])
        current   = {
            pos["ticker"]: Decimal(str(float(pos.get("position_fp") or 0)))
            for pos in positions
            if float(pos.get("position_fp") or 0) > 0
        }

        if self._prev_tickers is None:
            # First tick — just record current state, nothing to compare against.
            self._prev_tickers = set(current)
            self._prev_qtys    = dict(current)
            return

        settled_tickers = self._prev_tickers - set(current)
        old_qtys           = dict(self._prev_qtys)
        self._prev_tickers = set(current)
        self._prev_qtys    = dict(current)

        if not settled_tickers or not output_dir:
            return

        _FIELDS = ["ts", "symbol", "result"]
        path_csv = _Path(output_dir) / "settlements.csv"
        write_header = not path_csv.exists()

        with path_csv.open("a", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=_FIELDS)
            if write_header:
                writer.writeheader()

            for ticker in sorted(settled_tickers):
                try:
                    mkt = self._request("GET", f"/trade-api/v2/markets/{ticker}")
                    market = mkt.get("market", {})
                    result = market.get("result", "")
                    if result not in ("yes", "no", "void"):
                        continue  # not yet finalized
                    ts = datetime.now(tz=self._tz).strftime("%Y-%m-%d %H:%M")
                    writer.writerow({"ts": ts, "symbol": ticker, "result": result})
                    logger.info("Settled %s → %s", ticker, result.upper())
                except Exception as exc:
                    logger.warning("Settlement check failed for %s: %s", ticker, exc)

    def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        import requests

        url     = self._base_url + path
        headers = auth_headers(self._private_key, self._key_id, method, path)

        resp = requests.request(
            method, url, headers=headers, json=json_body, timeout=10
        )
        if resp.status_code not in (200, 201):
            raise TradingClientError(
                f"Kalshi API {method} {path} → {resp.status_code}: {resp.text}"
            )
        return resp.json()
