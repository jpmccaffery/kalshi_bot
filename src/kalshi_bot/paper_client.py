"""
PaperTradingClient — simulated trading client for paper trading.

Maintains a virtual cash balance and position book in memory.
Prices come from the real Kalshi data feed; no orders are sent to the exchange.

Fill model
----------
Buy:  fills at limit_price (full quantity, provided sufficient balance)
Sell: fills at limit_price (full quantity held)

Settlement
----------
Each tick, GET /portfolio/settlements is queried for any markets that have
resolved since the last check.  Paper positions that match are closed and
the virtual balance is credited ($1.00 per contract if YES, $0 if NO/void).
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from trading_bot.models import Order, OrderResult

from kalshi_bot.auth import auth_headers, load_private_key

logger = logging.getLogger(__name__)

_POSITION_COLS = ["symbol", "quantity", "avg_entry_price", "entry_ts", "original_edge"]

_DEMO_BASE = "https://demo-api.kalshi.co"
_LIVE_BASE = "https://api.elections.kalshi.com"


class PaperTradingClient:
    """
    Simulated trading client.  Implements TradingClientProtocol.

    Parameters
    ----------
    starting_balance:
        Initial virtual cash balance in dollars.
    key_id:
        Kalshi API key ID (used only for settlement queries).
    private_key_path:
        Path to RSA private key PEM file (used only for settlement queries).
    demo:
        If True, query the demo API for settlements.
    tz:
        Timezone for entry_ts timestamps.
    """

    def __init__(
        self,
        starting_balance:  Decimal = Decimal("10000"),
        key_id:            str     = "",
        private_key_path:  str     = "",
        demo:              bool    = False,
        tz:                ZoneInfo = ZoneInfo("UTC"),
        output_dir:        Path | None = None,
    ) -> None:
        self._balance     = starting_balance
        self._tz          = tz
        self._base_url    = _DEMO_BASE if demo else _LIVE_BASE
        self._key_id      = key_id
        self._private_key = load_private_key(private_key_path) if private_key_path else None
        self._positions:  dict[str, dict] = {}
        self._output_dir  = Path(output_dir) if output_dir else None

        logger.info(
            "PaperTradingClient initialised: starting_balance=$%.2f",
            float(starting_balance),
        )

    # ------------------------------------------------------------------
    # TradingClientProtocol
    # ------------------------------------------------------------------

    def place_limit_order(self, order: Order) -> OrderResult:
        qty   = Decimal(str(order.quantity)).quantize(Decimal("1"))
        price = Decimal(str(order.limit_price))

        if qty <= 0:
            return self._result(order.order_id, "rejected", Decimal("0"), None)

        if order.side == "buy":
            cost = qty * price
            if cost > self._balance:
                qty  = (self._balance / price).quantize(Decimal("1"))
                cost = qty * price
                if qty <= 0:
                    logger.warning(
                        "Paper BUY %s: insufficient balance ($%.2f) — skipping",
                        order.symbol, float(self._balance),
                    )
                    return self._result(order.order_id, "rejected", Decimal("0"), None)

            self._balance -= cost
            pos = self._positions.get(order.symbol)
            if pos:
                pos["qty"]        += qty
                pos["cost_basis"] += cost
            else:
                self._positions[order.symbol] = {
                    "qty":        qty,
                    "cost_basis": cost,
                    "entry_ts":   datetime.now(tz=self._tz),
                }

            logger.info(
                "Paper BUY  %s  qty=%s  price=$%.4f  cost=$%.2f  balance=$%.2f",
                order.symbol, qty, float(price), float(cost), float(self._balance),
            )
            return self._result(order.order_id, "filled", qty, price)

        else:  # sell
            pos = self._positions.get(order.symbol)
            if not pos or pos["qty"] <= 0:
                logger.warning("Paper SELL %s: no position held — skipping", order.symbol)
                return self._result(order.order_id, "rejected", Decimal("0"), None)

            sell_qty = min(qty, pos["qty"])
            proceeds = sell_qty * price
            self._balance     += proceeds
            pos["qty"]        -= sell_qty
            pos["cost_basis"] -= pos["cost_basis"] * sell_qty / (pos["qty"] + sell_qty)

            if pos["qty"] <= 0:
                del self._positions[order.symbol]

            logger.info(
                "Paper SELL %s  qty=%s  price=$%.4f  proceeds=$%.2f  balance=$%.2f",
                order.symbol, sell_qty, float(price), float(proceeds), float(self._balance),
            )
            return self._result(order.order_id, "filled", sell_qty, price)

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self) -> pd.DataFrame:
        self._settle_expired()

        if not self._positions:
            return pd.DataFrame(columns=_POSITION_COLS)

        rows = []
        for ticker, pos in self._positions.items():
            qty = pos["qty"]
            if qty <= 0:
                continue
            rows.append({
                "symbol":          ticker,
                "quantity":        qty,
                "avg_entry_price": pos["cost_basis"] / qty,
                "entry_ts":        pos["entry_ts"],
                "original_edge":   float("nan"),
            })

        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=_POSITION_COLS)

    def get_balance(self) -> Decimal:
        return self._balance

    def held_tickers(self) -> set[str]:
        """Return tickers of currently held paper positions without triggering settlement."""
        return set(self._positions.keys())

    def update_prices(self, bars: pd.DataFrame) -> None:
        pass

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def _settle_expired(self) -> None:
        """Check each held ticker's market status directly.

        portfolio/settlements only returns real-account settlements, so it is
        useless for paper trading.  Instead we query GET /markets/{ticker} for
        every position and settle any that are finalized.
        """
        if not self._private_key or not self._key_id or not self._positions:
            return

        to_settle: list[tuple[str, str]] = []  # (ticker, result)
        for ticker in list(self._positions):
            path = f"/trade-api/v2/markets/{ticker}"
            try:
                headers = auth_headers(self._private_key, self._key_id, "GET", path)
                resp    = requests.get(
                    self._base_url + path, headers=headers, timeout=10
                )
                if resp.status_code != 200:
                    logger.debug("Market fetch %s → %s", ticker, resp.status_code)
                    continue
                market = resp.json().get("market", {})
                if market.get("status") == "finalized":
                    to_settle.append((ticker, market.get("result", "no")))
            except Exception as exc:
                logger.warning("Market status fetch error for %s: %s", ticker, exc)

        for ticker, result in to_settle:
            pos  = self._positions.pop(ticker)
            qty  = pos["qty"]
            cost = pos["cost_basis"]
            ts   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

            if result == "yes":
                payout         = qty * Decimal("1.00")
                self._balance += payout
                pnl            = payout - cost
                logger.info(
                    "Paper SETTLE %s → YES  qty=%s  payout=$%.2f  pnl=%+.2f  balance=$%.2f",
                    ticker, qty, float(payout), float(pnl), float(self._balance),
                )
                self._write_settlement(ts, ticker, "yes", qty, cost, payout, pnl)
            elif result == "void":
                self._balance += cost
                payout = cost
                pnl    = Decimal("0")
                logger.info(
                    "Paper SETTLE %s → VOID  refund=$%.2f  balance=$%.2f",
                    ticker, float(cost), float(self._balance),
                )
                self._write_settlement(ts, ticker, "void", qty, cost, payout, pnl)
            else:  # "no"
                payout = Decimal("0")
                pnl    = -cost
                logger.info(
                    "Paper SETTLE %s → NO  qty=%s  pnl=%+.2f  balance=$%.2f",
                    ticker, qty, float(pnl), float(self._balance),
                )
                self._write_settlement(ts, ticker, "no", qty, cost, payout, pnl)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    _SETTLEMENT_FIELDS = ["ts", "symbol", "result", "quantity",
                          "cost_basis", "payout", "pnl"]

    def _write_settlement(
        self,
        ts: str,
        ticker: str,
        result: str,
        qty: Decimal,
        cost: Decimal,
        payout: Decimal,
        pnl: Decimal,
    ) -> None:
        if not self._output_dir:
            return
        path = self._output_dir / "settlements.csv"
        write_header = not path.exists()
        with path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self._SETTLEMENT_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "ts":         ts,
                "symbol":     ticker,
                "result":     result,
                "quantity":   int(qty),
                "cost_basis": round(float(cost), 4),
                "payout":     round(float(payout), 4),
                "pnl":        round(float(pnl), 4),
            })

    @staticmethod
    def _result(
        order_id:   str,
        status:     str,
        filled_qty: Decimal,
        price:      Decimal | None,
    ) -> OrderResult:
        return OrderResult(
            order_id       = order_id,
            status         = status,
            filled_qty     = filled_qty,
            avg_fill_price = price,
            fees_paid      = Decimal("0"),
            exchange_ts    = datetime.now(tz=timezone.utc),
            raw_response   = {},
        )
