"""
Market discovery — fetches and caches the universe of open markets from Kalshi.

Architecture:
  - Single paginated call to GET /markets with status=open and a close-time
    window (e.g. now → now+12h). This is far cheaper than querying per series.
  - Results are stored in an in-memory cache.
  - refresh() is intended to run on a slow cadence (e.g. hourly) since the
    open universe changes slowly. The trading loop reads from the cache.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from src.kalshi.client import KalshiClient
from src.kalshi.models import Market


class MarketDiscovery(ABC):
    @abstractmethod
    def refresh(self) -> list[Market]:
        """
        Fetch open markets from the API and update the internal cache.
        Returns the full cached list after refreshing.
        """

    @abstractmethod
    def get_cached(self) -> list[Market]:
        """
        Return the last cached market list without making any API calls.
        Returns an empty list if refresh() has never been called.
        """

    @property
    def default_window(self) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        return now, now + timedelta(hours=48)


class KalshiMarketDiscovery(MarketDiscovery):
    """
    Pages through GET /markets?status=open with a configurable close-time window.
    One API call per page — no per-series calls needed.
    """

    def __init__(self, client: KalshiClient, window_hours: int = 12, max_pages: int = 20):
        self._client = client
        self._window_hours = window_hours
        self._max_pages = max_pages
        self._cache: list[Market] = []

    def refresh(self) -> list[Market]:
        now = datetime.now(timezone.utc)
        min_ts = now
        max_ts = now + timedelta(hours=self._window_hours)

        markets = []
        cursor = None
        for _ in range(self._max_pages):
            params = {
                "status": "open",
                "min_close_ts": int(min_ts.timestamp()),
                "max_close_ts": int(max_ts.timestamp()),
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            resp = self._client._get("/markets", params=params)
            for m in resp.get("markets", []):
                try:
                    bid = round(float(m.get("yes_bid_dollars") or 0) * 100)
                    ask = round(float(m.get("yes_ask_dollars") or 0) * 100)
                    markets.append(Market(
                        ticker=m["ticker"],
                        title=m.get("title", ""),
                        status=m.get("status", ""),
                        yes_bid=bid,
                        yes_ask=ask,
                        volume=round(float(m.get("volume_fp") or 0)),
                        open_interest=round(float(m.get("open_interest_fp") or 0)),
                        close_time=m.get("close_time"),
                        series_ticker=m.get("series_ticker"),
                        event_ticker=m.get("event_ticker"),
                        volume_24h=round(float(m.get("volume_24h_fp") or 0)),
                        liquidity_cents=round(float(m.get("liquidity_dollars") or 0) * 100),
                    ))
                except (TypeError, ValueError, KeyError):
                    continue
            cursor = resp.get("cursor")
            if not cursor:
                break

        self._cache = markets
        return markets

    def get_cached(self) -> list[Market]:
        return self._cache
