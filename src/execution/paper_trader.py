"""
Paper trader — simulates order execution against live market data.

Fills are assumed at the current mid price (optimistic but simple). Tracks
positions and running P&L, and appends each fill to logs/paper_fills.csv.
"""

import csv
import logging
import os
from datetime import datetime, timezone

from src.config import RiskConfig
from src.execution.base import Executor
from src.kalshi.models import Fill, Market, OrderAction, Position, Side, Signal

logger = logging.getLogger(__name__)

FILLS_LOG = "logs/paper_fills.csv"
_CSV_HEADERS = ["timestamp", "ticker", "side", "action", "quantity", "price_cents", "pnl_cents"]


class PaperTrader(Executor):
    def __init__(self, risk: RiskConfig, starting_balance_cents: int = 100_000):
        super().__init__(risk)
        self.balance_cents = starting_balance_cents
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        os.makedirs("logs", exist_ok=True)
        self._init_csv()

    def _init_csv(self) -> None:
        if not os.path.exists(FILLS_LOG):
            with open(FILLS_LOG, "w", newline="") as f:
                csv.writer(f).writerow(_CSV_HEADERS)

    def _execute(self, signals: list[Signal], markets: dict[str, Market]) -> list[Fill]:
        fills = []
        for signal in signals:
            market = markets.get(signal.ticker)
            if market is None:
                logger.warning("No market data for %s — skipping signal", signal.ticker)
                continue

            fill_price = self._fill_price(signal, market)
            fill = Fill(
                ticker=signal.ticker,
                side=signal.side,
                action=signal.action,
                quantity=signal.quantity,
                price=fill_price,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            pnl = self._update_position(fill)
            self._update_balance(fill, pnl)
            self._log_fill(fill, pnl)
            fills.append(fill)

            logger.info(
                "[PAPER] %s %s %s x%d @ %dc | balance=$%.2f",
                fill.action.value.upper(),
                fill.side.value.upper(),
                fill.ticker,
                fill.quantity,
                fill.price,
                self.balance_cents / 100,
            )

        return fills

    def _fill_price(self, signal: Signal, market: Market) -> int:
        if signal.limit_price is not None:
            return signal.limit_price
        # Fill at mid price for market orders
        if signal.side == Side.YES:
            return round(market.yes_mid)
        return round(market.no_mid)

    def _update_position(self, fill: Fill) -> int:
        """Update position and return realized P&L in cents (positive = profit)."""
        pos = self.positions.setdefault(fill.ticker, Position(ticker=fill.ticker))
        pnl = 0

        if fill.side == Side.YES:
            if fill.action == OrderAction.BUY:
                pos.yes_quantity += fill.quantity
            else:
                closed = min(fill.quantity, pos.yes_quantity)
                pos.yes_quantity -= closed
                # Simplified: realized pnl tracked externally; here we record cost basis delta
                pnl = (fill.price - 50) * closed  # rough proxy
                pos.realized_pnl_cents += pnl
        else:
            if fill.action == OrderAction.BUY:
                pos.no_quantity += fill.quantity
            else:
                closed = min(fill.quantity, pos.no_quantity)
                pos.no_quantity -= closed
                pnl = (fill.price - 50) * closed
                pos.realized_pnl_cents += pnl

        if pnl < 0:
            self.record_loss(-pnl)
        return pnl

    def _update_balance(self, fill: Fill, pnl: int) -> None:
        cost = fill.price * fill.quantity
        if fill.action == OrderAction.BUY:
            self.balance_cents -= cost
        else:
            self.balance_cents += cost

    def _log_fill(self, fill: Fill, pnl: int) -> None:
        with open(FILLS_LOG, "a", newline="") as f:
            csv.writer(f).writerow([
                fill.timestamp, fill.ticker, fill.side.value,
                fill.action.value, fill.quantity, fill.price, pnl,
            ])

    def summary(self) -> None:
        logger.info("=== Paper Trading Summary ===")
        logger.info("Balance: $%.2f", self.balance_cents / 100)
        for ticker, pos in self.positions.items():
            logger.info(
                "  %s: YES=%d NO=%d realized_pnl=$%.2f",
                ticker, pos.yes_quantity, pos.no_quantity, pos.realized_pnl_cents / 100,
            )
