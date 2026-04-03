"""
Paper trader — simulates order execution against live market data.

Fills at the signal's limit_price, or mid price for market orders.
Portfolio state (positions, balance, P&L) is managed by the shared Portfolio.
Each fill is appended to logs/paper_fills.csv.
"""

import csv
import logging
import os
from datetime import datetime, timezone

from src.config import RiskConfig
from src.execution.base import Executor
from src.kalshi.models import Fill, Market, Side, Signal
from src.portfolio import Portfolio

logger = logging.getLogger(__name__)

FILLS_LOG = "logs/paper_fills.csv"
_CSV_HEADERS = ["timestamp", "ticker", "side", "action", "quantity", "price_cents", "kelly_fraction"]


class PaperTrader(Executor):
    def __init__(self, risk: RiskConfig, portfolio: Portfolio):
        super().__init__(risk, portfolio)
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
            self._log_fill(fill, signal.kelly_fraction)
            fills.append(fill)

            logger.info(
                "[PAPER] %s %s %s x%d @ %dc  kelly=%.3f  balance=$%.2f",
                fill.action.value.upper(),
                fill.side.value.upper(),
                fill.ticker,
                fill.quantity,
                fill.price,
                signal.kelly_fraction,
                self.portfolio.balance_cents / 100,
            )

        return fills

    def _fill_price(self, signal: Signal, market: Market) -> int:
        if signal.limit_price is not None:
            return signal.limit_price
        if signal.side == Side.YES:
            return round(market.yes_mid)
        return round(market.no_mid)

    def _log_fill(self, fill: Fill, kelly_fraction: float) -> None:
        with open(FILLS_LOG, "a", newline="") as f:
            csv.writer(f).writerow([
                fill.timestamp, fill.ticker, fill.side.value,
                fill.action.value, fill.quantity, fill.price, f"{kelly_fraction:.4f}",
            ])

    def summary(self) -> None:
        logger.info("=== Paper Trading Summary ===")
        self.portfolio.summary()
