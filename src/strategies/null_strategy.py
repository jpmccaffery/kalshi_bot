"""
Null strategy — never trades.

Useful for validating the plumbing end-to-end: it will log the market state
it receives each iteration without placing any orders.
"""

import logging

from src.kalshi.models import Market, Signal
from src.strategies.base import Strategy

logger = logging.getLogger(__name__)


class NullStrategy(Strategy):
    def generate_signals(self, markets: dict[str, Market]) -> list[Signal]:
        for ticker, market in markets.items():
            logger.info(
                "[%s] %s | yes_bid=%dc yes_ask=%dc mid=%.1fc | volume=%d oi=%d",
                ticker,
                market.title,
                market.yes_bid,
                market.yes_ask,
                market.yes_mid,
                market.volume,
                market.open_interest,
            )
        return []
