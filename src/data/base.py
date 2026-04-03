"""
Data feed abstraction.

The strategy receives a dict[ticker, Market] snapshot each loop iteration.
To add a new data source (news, weather, etc.) subclass DataFeed and attach
it to your strategy directly — the core loop stays unchanged.
"""

from abc import ABC, abstractmethod

from src.kalshi.client import KalshiClient
from src.kalshi.models import Market


class DataFeed(ABC):
    @abstractmethod
    def fetch(self, tickers: list[str]) -> dict[str, Market]:
        """Return the latest Market snapshot for each ticker."""


class KalshiDataFeed(DataFeed):
    def __init__(self, client: KalshiClient):
        self._client = client

    def fetch(self, tickers: list[str]) -> dict[str, Market]:
        markets = self._client.get_markets(tickers)
        return {m.ticker: m for m in markets}
