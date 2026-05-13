"""
KalshiDataFeed — live data feed for the Kalshi prediction-market exchange.

Implements DataFeedProtocol from the trading_bot framework.

Fetches the current orderbook snapshot for each configured market ticker
via the Kalshi REST API.  Prices are read from Kalshi's ``*_dollars``
fields and stored as floats in [0.00, 1.00] (probability space).

Schema provided (per bar):
    symbol          object      market ticker
    ts              datetime64  fetch timestamp (UTC)
    yes_bid         float64     best bid for YES contracts  (0–0.99)
    yes_ask         float64     best ask for YES contracts  (0–0.99)
    no_bid          float64     best bid for NO contracts   (0–0.99)
    no_ask          float64     best ask for NO contracts   (0–0.99)
    volume          float64     total contracts traded
    open_interest   float64     open contracts outstanding

Market resolution
-----------------
Use ``resolve_symbols(pattern, ...)`` to discover tickers matching a regex
pattern from the live Kalshi /markets endpoint, rather than hard-coding a
symbol list.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.data_feed import LiveDataFeed
from trading_bot.exceptions import DataFeedError
from trading_bot.models import DataSchema

from kalshi_bot.auth import auth_headers, load_private_key

logger = logging.getLogger(__name__)

KALSHI_SCHEMA = DataSchema(
    columns={
        "symbol":        "object",
        "ts":            "datetime64",
        "yes_bid":       "float64",
        "yes_ask":       "float64",
        "no_bid":        "float64",
        "no_ask":        "float64",
        "volume":        "float64",
        "open_interest": "float64",
    }
)

_DEMO_BASE = "https://demo-api.kalshi.co"
_LIVE_BASE = "https://api.elections.kalshi.com"


class KalshiDataFeed(LiveDataFeed):
    """
    Live data feed for Kalshi prediction markets.

    Parameters
    ----------
    symbols:
        List of Kalshi market tickers (e.g. ["KXINX-24", "KXETHD-24"]).
    key_id:
        Kalshi API key ID.
    private_key_path:
        Path to the RSA private key PEM file.
    lookback_bars:
        Number of historical bars to retain in the snapshot history window.
    demo:
        If True (default), use the Kalshi demo API.
    tz:
        Timezone for timestamps (default UTC).
    """

    def __init__(
        self,
        symbols: list[str],
        key_id: str,
        private_key_path: str,
        series_tickers: list[str] | None = None,
        lookback_bars: int = 20,
        demo: bool = True,
        tz: ZoneInfo = ZoneInfo("UTC"),
    ) -> None:
        super().__init__(symbols=symbols, lookback_bars=lookback_bars, tz=tz)
        self._base_url      = _DEMO_BASE if demo else _LIVE_BASE
        self._key_id        = key_id
        self._private_key   = load_private_key(private_key_path)
        self._series_tickers = list(series_tickers or [])
        self._pinned: set[str] = set()

    def pin_tickers(self, tickers: set[str]) -> None:
        """Ensure these tickers are always fetched, even if not in discovered markets.

        Call after each tick with currently held paper positions so the next
        tick's price snapshot always includes them for sell evaluation.
        """
        self._pinned = tickers

    @property
    def provided_schema(self) -> DataSchema:
        return KALSHI_SCHEMA

    def _position_tickers(self) -> set[str]:
        """Return tickers of currently held positions from the portfolio API."""
        import requests
        path    = "/trade-api/v2/portfolio/positions"
        headers = auth_headers(self._private_key, self._key_id, "GET", path)
        resp    = requests.get(self._base_url + path, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning("Could not fetch positions to extend price snapshot: %s", resp.text)
            return set()
        return {
            p["ticker"]
            for p in resp.json().get("market_positions", [])
            if p.get("ticker") and float(p.get("position_fp", 0) or 0) > 0
        }

    def _fetch_raw(self) -> list[dict]:
        import requests
        from tqdm import tqdm

        path    = "/trade-api/v2/markets"
        results: list[dict] = []

        if self._series_tickers:
            # Re-discover all open markets by series each tick, then fetch their prices.
            discovered: set[str] = set()
            with tqdm(self._series_tickers, desc="Fetching prices", unit="series") as bar:
                for series_ticker in bar:
                    bar.set_postfix_str(series_ticker)
                    cursor = None
                    while True:
                        params  = {"series_ticker": series_ticker, "status": "open", "limit": 200}
                        if cursor:
                            params["cursor"] = cursor
                        headers = auth_headers(self._private_key, self._key_id, "GET", path)
                        resp    = requests.get(
                            self._base_url + path, headers=headers,
                            params=params, timeout=10,
                        )
                        if resp.status_code != 200:
                            logger.warning("Series fetch failed for %s: %s",
                                           series_ticker, resp.status_code)
                            break
                        body    = resp.json()
                        markets = body.get("markets", [])
                        for market in markets:
                            ticker = market.get("ticker", "")
                            if ticker:
                                market["_ticker"] = ticker
                                results.append(market)
                                discovered.add(ticker)
                        cursor = body.get("cursor") or None
                        if not cursor or not markets:
                            break

            # Update the framework's symbol list to match current open markets.
            self._symbols = sorted(discovered)

            # The framework's base class maintains a _cache dict keyed by symbol,
            # initialised only for symbols passed at __init__. Pre-register any
            # newly discovered symbols so the framework can store their bars.
            for ticker in discovered:
                if ticker not in self._cache:
                    self._cache[ticker] = []

            # Fetch prices for portfolio tickers that have fallen out of open markets
            # (e.g. contracts that closed but we still hold). These come from the real
            # portfolio API plus any pinned paper positions.
            portfolio_tickers = self._position_tickers() | self._pinned
            extra = portfolio_tickers - discovered
        else:
            # Static symbol list (KALSHI_MARKETS explicit tickers path).
            portfolio_tickers = self._position_tickers() | self._pinned
            extra = (set(self._symbols) | portfolio_tickers)

        if extra:
            events: dict[str, list[str]] = {}
            for ticker in extra:
                event_ticker = ticker.rsplit("-", 1)[0]
                events.setdefault(event_ticker, []).append(ticker)

            for event_ticker, tickers in events.items():
                wanted = set(tickers)
                cursor = None
                while True:
                    params  = {"event_ticker": event_ticker, "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    headers = auth_headers(self._private_key, self._key_id, "GET", path)
                    resp    = requests.get(
                        self._base_url + path, headers=headers,
                        params=params, timeout=10,
                    )
                    if resp.status_code != 200:
                        raise DataFeedError(
                            f"Kalshi API error {resp.status_code} "
                            f"for event {event_ticker}: {resp.text}"
                        )
                    body    = resp.json()
                    markets = body.get("markets", [])
                    for market in markets:
                        ticker = market.get("ticker", "")
                        if ticker in wanted:
                            market["_ticker"] = ticker
                            results.append(market)
                    cursor = body.get("cursor") or None
                    if not cursor or not markets:
                        break

        if self._series_tickers:
            logger.info("Fetched %d markets (%d open, %d portfolio-only)",
                        len(results), len(discovered), len(extra))

        return results

    def _to_dataframe(self, raw: list[dict]) -> pd.DataFrame:
        rows = []
        now  = datetime.now(tz=self._tz)
        for market in raw:
            yes_ask = _to_float(market.get("yes_ask_dollars"))
            if yes_ask <= 0 or yes_ask != yes_ask:
                ticker = market.get("_ticker", market.get("ticker", "?"))
                relevant = {k: market[k] for k in (
                    "yes_bid", "yes_ask", "yes_bid_dollars", "yes_ask_dollars",
                    "no_bid", "no_ask", "status",
                ) if k in market}
                logger.debug("Zero/NaN yes_ask for %s — raw fields: %s", ticker, relevant)
            rows.append({
                "symbol":        market.get("_ticker", market.get("ticker", "")),
                "ts":            now,
                "yes_bid":       _to_float(market.get("yes_bid_dollars")),
                "yes_ask":       yes_ask,
                "no_bid":        _to_float(market.get("no_bid_dollars")),
                "no_ask":        _to_float(market.get("no_ask_dollars")),
                "volume":        _require_float(market, "volume_fp"),
                "open_interest": _require_float(market, "open_interest_fp"),
            })
        return pd.DataFrame(rows)


def resolve_symbols(
    series: list[str],
    key_id: str,
    private_key_path: str,
    demo: bool = True,
    page_size: int = 200,
) -> list[str]:
    """
    Return all open market tickers for the given series tickers.

    Uses a two-step server-side approach to avoid paginating the full
    market catalogue:

      1. ``GET /events?series_ticker=X&status=open``  →  event tickers
      2. ``GET /markets?event_ticker=Y&status=open``  →  market tickers

    Parameters
    ----------
    series:
        List of series tickers, e.g. ``["KXINX", "KXEURUSD"]``.
    key_id:
        Kalshi API key ID.
    private_key_path:
        Path to RSA private key PEM file.
    demo:
        If True, use the demo API base URL.
    page_size:
        Items per page for each paginated call.

    Returns
    -------
    list[str]
        Sorted list of market tickers across all series.

    Raises
    ------
    DataFeedError
        If any API call returns a non-200 status.
    """
    import requests

    base_url    = _DEMO_BASE if demo else _LIVE_BASE
    private_key = load_private_key(private_key_path)

    def _get_pages(path: str, params: dict) -> list[dict]:
        """Fetch all pages from a paginated endpoint."""
        results = []
        cursor: str | None = None
        while True:
            page_params = {**params, "limit": page_size}
            if cursor:
                page_params["cursor"] = cursor
            headers = auth_headers(private_key, key_id, "GET", path)
            resp    = requests.get(base_url + path, headers=headers,
                                   params=page_params, timeout=10)
            if resp.status_code != 200:
                raise DataFeedError(
                    f"Kalshi API error {resp.status_code} on {path}: {resp.text}"
                )
            body   = resp.json()
            # collection key is the last path segment (e.g. "events", "markets")
            key    = path.rstrip("/").split("/")[-1]
            items  = body.get(key, [])
            results.extend(items)
            cursor = body.get("cursor") or None
            if not cursor or not items:
                break
        return results

    all_tickers: list[str] = []

    import time
    from tqdm import tqdm

    with tqdm(series, desc="Resolving markets", unit="series") as bar:
        for series_ticker in bar:
            bar.set_postfix_str(series_ticker)
            time.sleep(0.3)

            events = _get_pages(
                "/trade-api/v2/events",
                {"series_ticker": series_ticker, "status": "open"},
            )

            for event in events:
                event_ticker = event.get("event_ticker", "")
                if not event_ticker:
                    continue
                markets = _get_pages(
                    "/trade-api/v2/markets",
                    {"event_ticker": event_ticker, "status": "open"},
                )
                all_tickers.extend(
                    m.get("ticker", "") for m in markets if m.get("ticker")
                )

    all_tickers.sort()
    logger.info(
        "resolve_symbols(%s): found %d market tickers",
        ", ".join(series), len(all_tickers),
    )
    return all_tickers


def _to_float(value, default: float = float("nan")) -> float:
    """Convert a Kalshi price field (string or numeric) to a float."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _require_float(market: dict, key: str) -> float:
    """Return float value for key, warning loudly if the key is absent."""
    if key not in market:
        ticker = market.get("_ticker", market.get("ticker", "?"))
        logger.warning("Market %s missing expected field %r — got 0.0", ticker, key)
        return 0.0
    return _to_float(market[key], default=0.0)
