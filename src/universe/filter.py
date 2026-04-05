"""
Universe filtering — narrows a raw list of markets down to the tradeable set.

Filters are stateless and composable via FilterChain.
"""

from abc import ABC, abstractmethod

from src.kalshi.models import Market


class UniverseFilter(ABC):
    @abstractmethod
    def apply(self, markets: list[Market]) -> list[Market]:
        """Return the subset of markets that pass this filter."""


class FilterChain(UniverseFilter):
    """Applies a sequence of filters in order. A market must pass all of them."""

    def __init__(self, filters: list[UniverseFilter]):
        self._filters = filters

    def apply(self, markets: list[Market]) -> list[Market]:
        result = markets
        for f in self._filters:
            result = f.apply(result)
        return result


class LiquidFilter(UniverseFilter):
    """Keeps markets with a real two-sided market (bid > 0 and ask < 100)."""
    def apply(self, markets: list[Market]) -> list[Market]:
        return [m for m in markets if m.yes_bid > 0 and m.yes_ask < 100]


class MinVolumeFilter(UniverseFilter):
    def __init__(self, min_volume: int):
        self._min = min_volume
    def apply(self, markets: list[Market]) -> list[Market]:
        return [m for m in markets if m.volume >= self._min]


class MaxSpreadFilter(UniverseFilter):
    """Keeps markets where spread (yes_ask - yes_bid) <= max_cents."""
    def __init__(self, max_cents: int):
        self._max = max_cents
    def apply(self, markets: list[Market]) -> list[Market]:
        return [m for m in markets if (m.yes_ask - m.yes_bid) <= self._max]


class ExcludePrefixFilter(UniverseFilter):
    """Drops tickers starting with any of the given prefixes (e.g. parlay markets)."""
    def __init__(self, prefixes: list[str]):
        self._prefixes = prefixes
    def apply(self, markets: list[Market]) -> list[Market]:
        return [m for m in markets if not any(m.ticker.startswith(p) for p in self._prefixes)]


class SeriesWhitelistFilter(UniverseFilter):
    """Only keeps markets belonging to the given series tickers."""
    def __init__(self, series: list[str]):
        self._series = set(series)
    def apply(self, markets: list[Market]) -> list[Market]:
        return [m for m in markets if m.series_ticker in self._series]
