"""
Portfolio — single source of truth for cash balance and open positions.

Both the KellySizer (reads state to make sizing decisions) and the Executor
(writes state after each fill) share this object.
"""

import logging
from dataclasses import dataclass, field

from src.kalshi.models import Fill, Market, OrderAction, Position, Side

logger = logging.getLogger(__name__)


@dataclass
class Portfolio:
    balance_cents: int
    positions: dict[str, Position] = field(default_factory=dict)

    def yes_quantity(self, ticker: str) -> int:
        return self.positions.get(ticker, Position(ticker=ticker)).yes_quantity

    def no_quantity(self, ticker: str) -> int:
        return self.positions.get(ticker, Position(ticker=ticker)).no_quantity

    def apply_fill(self, fill: Fill) -> int:
        """
        Update balance and position from a fill.
        Returns realized P&L in cents (positive = profit, negative = loss).
        """
        pos = self.positions.setdefault(fill.ticker, Position(ticker=fill.ticker))
        pnl = 0

        if fill.side == Side.YES:
            pnl = self._apply_side(
                fill, pos.yes_quantity, pos.yes_avg_cost,
                is_yes=True,
            )
        else:
            pnl = self._apply_side(
                fill, pos.no_quantity, pos.no_avg_cost,
                is_yes=False,
            )

        if pnl != 0:
            pos.realized_pnl_cents += pnl
        return pnl

    def _apply_side(self, fill: Fill, qty: int, avg_cost: float, is_yes: bool) -> int:
        pos = self.positions[fill.ticker]
        pnl = 0

        if fill.action == OrderAction.BUY:
            new_qty = qty + fill.quantity
            new_avg = (qty * avg_cost + fill.price * fill.quantity) / new_qty
            if is_yes:
                pos.yes_quantity = new_qty
                pos.yes_avg_cost = new_avg
            else:
                pos.no_quantity = new_qty
                pos.no_avg_cost = new_avg
            self.balance_cents -= fill.price * fill.quantity

        else:  # SELL
            closed = min(fill.quantity, qty)
            pnl = round((fill.price - avg_cost) * closed)
            if is_yes:
                pos.yes_quantity = qty - closed
                if pos.yes_quantity == 0:
                    pos.yes_avg_cost = 0.0
            else:
                pos.no_quantity = qty - closed
                if pos.no_quantity == 0:
                    pos.no_avg_cost = 0.0
            self.balance_cents += fill.price * closed

        return pnl

    def total_value_cents(self, markets: dict[str, Market]) -> int:
        """Cash + mark-to-market value of all open positions."""
        value = self.balance_cents
        for ticker, pos in self.positions.items():
            market = markets.get(ticker)
            if market is None:
                continue
            value += pos.yes_quantity * market.yes_bid
            value += pos.no_quantity * market.no_bid
        return value

    def summary(self) -> None:
        logger.info("Balance: $%.2f", self.balance_cents / 100)
        for ticker, pos in self.positions.items():
            if pos.yes_quantity or pos.no_quantity:
                logger.info(
                    "  %s: YES=%d (avg=%.1fc) NO=%d (avg=%.1fc) realized_pnl=$%.2f",
                    ticker,
                    pos.yes_quantity, pos.yes_avg_cost,
                    pos.no_quantity, pos.no_avg_cost,
                    pos.realized_pnl_cents / 100,
                )
