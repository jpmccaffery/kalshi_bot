"""
Null strategy — never trades.

Logs the market state each iteration but returns no estimates, so the sizer
produces no signals. Useful for validating the plumbing end-to-end.
"""

import logging

from src.kalshi.models import EdgeEstimate, Market
from src.strategies.base import Strategy

logger = logging.getLogger(__name__)


class NullStrategy(Strategy):
    def estimate_edge(self, markets: dict[str, Market]) -> list[EdgeEstimate]:
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
