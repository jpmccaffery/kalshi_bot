"""
Data feed abstraction.

The strategy receives a dict[ticker, Market] snapshot each loop iteration.
To add a new data source (news, weather, etc.) subclass DataFeed and attach
it to your strategy directly — the core loop stays unchanged.
"""

import logging
from abc import ABC, abstractmethod

from src.kalshi.client import KalshiClient
from src.kalshi.models import Market

log = logging.getLogger(__name__)


class DataFeed(ABC):
    @abstractmethod
    def fetch(self, tickers: list[str]) -> dict[str, Market]:
        """Return the latest Market snapshot for each ticker."""


class KalshiDataFeed(DataFeed):
    def __init__(self, client: KalshiClient):
        self._client = client

    def fetch(self, tickers: list[str]) -> dict[str, Market]:
        markets = self._client.get_markets(tickers)
        for m in markets:
            log.debug(
                "%s  yes_bid=%d  yes_ask=%d  no_bid=%d  no_ask=%d  vol=%d  oi=%d",
                m.ticker, m.yes_bid, m.yes_ask, m.no_bid, m.no_ask,
                m.volume, m.open_interest,
            )
        return {m.ticker: m for m in markets}
