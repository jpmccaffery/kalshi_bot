"""
Kalshi market data source client for the weather features pipeline.

Polls weather market series from Kalshi every 10 minutes.
Produces market_snapshots rows (one per open market per poll).
Detects market closures and produces market_results rows.

Auth: RSA-PSS via kalshi_bot.auth. Reads KALSHI_API_KEY_ID and
KALSHI_API_PRIVATE_KEY_PATH from env. Key path resolves relative to /app
(the Docker working directory).

Series list: read from KALSHI_SERIES env var (comma-separated).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import aiohttp

from kalshi_bot.auth import auth_headers, load_private_key

from .base import SourceClient

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com"

_EXPIRY_RE = re.compile(r"(\d{2}[A-Z]{3}\d{2})")
_MONTH = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_expiry(ticker: str) -> Optional[dt.date]:
    """Extract and parse the expiry date from a ticker like 'KXHIGHNY-26MAY14-B70'."""
    m = _EXPIRY_RE.search(ticker)
    if not m:
        return None
    s = m.group(1)
    try:
        return dt.date(2000 + int(s[:2]), _MONTH[s[2:5]], int(s[5:]))
    except (ValueError, KeyError):
        return None


def _load_key():
    """Load the Kalshi private key from KALSHI_API_PRIVATE_KEY_PATH."""
    key_path_raw = os.environ.get("KALSHI_API_PRIVATE_KEY_PATH", "")
    if not key_path_raw:
        raise RuntimeError("KALSHI_API_PRIVATE_KEY_PATH not set")
    key_path = Path(key_path_raw)
    if not key_path.is_absolute():
        # Resolve relative to /app (Docker working directory).
        key_path = Path("/app") / key_path
    return load_private_key(key_path)


def _get_key_id() -> str:
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    if not key_id:
        raise RuntimeError("KALSHI_API_KEY_ID not set")
    return key_id


def _compute_hash(data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


class KalshiMarketsSource(SourceClient):
    """
    Polls Kalshi weather series for open market snapshots and detects closures.

    Writes to two tables:
    - market_snapshots: one row per open market per poll
    - market_results: one row per finalized market (emitted once)

    The scheduler calls poll() which returns rows for the primary table
    (market_snapshots). market_results rows are written via a secondary write
    call in the scheduler using self.pending_results.
    """

    name = "KALSHI_MARKETS"
    table = "market_snapshots"
    min_poll_interval_sec = 600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None,
                 data_dir: Optional[Path] = None) -> None:
        super().__init__(semaphore)
        self._series: list[str] = [
            s.strip()
            for s in os.environ.get("KALSHI_SERIES", "").split(",")
            if s.strip()
        ]
        # Load auth credentials lazily to avoid failing on import.
        self._private_key = None
        self._key_id: Optional[str] = None
        self._auth_ready = False

        # Track previously-seen open tickers per series for closure detection.
        self._prev_open: dict[str, set[str]] = {}
        # Track tickers we've already emitted a market_results row for.
        # Persisted to disk so restarts don't re-emit historical results.
        self._seen_path: Optional[Path] = (
            data_dir / "market" / "market_results" / "_seen_tickers.json"
            if data_dir else None
        )
        self._emitted_results: set[str] = self._load_seen()
        # Tickers found in the current poll, not yet confirmed written.
        # Only moved into _emitted_results after storage.write() succeeds.
        self._pending_emitted: set[str] = set()
        # Pending market_results rows to be written by the scheduler.
        self.pending_results: list[dict] = []

    def _load_seen(self) -> set[str]:
        """Load persisted set of already-emitted result tickers from disk."""
        if self._seen_path and self._seen_path.exists():
            try:
                import json as _json
                return set(_json.loads(self._seen_path.read_text()))
            except Exception:
                pass
        return set()

    def _save_seen(self) -> None:
        """Persist the emitted-results set to disk."""
        if not self._seen_path:
            return
        try:
            import json as _json
            self._seen_path.parent.mkdir(parents=True, exist_ok=True)
            self._seen_path.write_text(_json.dumps(sorted(self._emitted_results)))
        except Exception as exc:
            log.warning("KALSHI_MARKETS: failed to save seen tickers: %s", exc)

    def confirm_results_written(self) -> None:
        """Call after market_results rows have been successfully written to storage.
        Only then move pending tickers into _emitted_results and persist to disk,
        so a failed write never marks tickers as seen prematurely."""
        self._emitted_results.update(self._pending_emitted)
        self._pending_emitted.clear()
        self._save_seen()

    def _init_auth(self) -> None:
        if self._auth_ready:
            return
        try:
            self._private_key = _load_key()
            self._key_id = _get_key_id()
            self._auth_ready = True
        except Exception as exc:
            raise RuntimeError(f"Kalshi auth init failed: {exc}") from exc

    def _make_headers(self, method: str, path: str) -> dict[str, str]:
        return auth_headers(self._private_key, self._key_id, method, path)

    async def _get_json(self, session: aiohttp.ClientSession,
                        path: str, params: Optional[dict] = None) -> dict:
        """Make an authenticated GET request and return parsed JSON."""
        qs = ("?" + urlencode(params)) if params else ""
        full_path = path + qs
        headers = self._make_headers("GET", path)  # sign without query string
        url = KALSHI_BASE + full_path
        if self._semaphore:
            async with self._semaphore:
                async with session.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        else:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.json()

    def _market_to_snapshot_row(self, market: dict, poll_time: dt.datetime,
                                series: str, raw_hash: str) -> dict:
        """Convert a Kalshi market dict to a market_snapshots row."""
        ticker = market.get("ticker", "")
        expiry_date = _parse_expiry(ticker)

        # Price fields come back as dollar strings e.g. "yes_bid_dollars": "0.1200"
        def _to_int(v) -> Optional[int]:
            if v is None:
                return None
            try:
                return int(float(v))
            except (ValueError, TypeError):
                return None

        def _price(key: str) -> Optional[float]:
            v = market.get(key)
            if v is None:
                return None
            try:
                f = float(v)
                return f if f == f else None  # drop NaN
            except (ValueError, TypeError):
                return None

        return {
            "poll_time": poll_time,
            "source": self.name,
            "ticker": ticker,
            "series": series,
            "expiry_date": expiry_date,
            "yes_bid": _price("yes_bid_dollars"),
            "yes_ask": _price("yes_ask_dollars"),
            "no_bid": _price("no_bid_dollars"),
            "no_ask": _price("no_ask_dollars"),
            "last_price": _price("last_price_dollars"),
            "volume": _to_int(market.get("volume_fp")),
            "open_interest": _to_int(market.get("open_interest_fp")),
            "status": market.get("status"),
            "raw_payload_hash": raw_hash,
            "schema_version": 1,
        }

    def _market_to_result_row(self, market: dict, poll_time: dt.datetime,
                              series: str, raw_hash: str) -> dict:
        """Convert a finalized Kalshi market dict to a market_results row."""
        ticker = market.get("ticker", "")
        expiry_date = _parse_expiry(ticker)

        close_time_raw = market.get("close_time") or market.get("expiration_time")
        close_time: Optional[dt.datetime] = None
        if close_time_raw:
            try:
                close_time = dt.datetime.fromisoformat(
                    str(close_time_raw).replace("Z", "+00:00")
                )
                if close_time.tzinfo is None:
                    close_time = close_time.replace(tzinfo=dt.timezone.utc)
            except (ValueError, TypeError):
                close_time = None

        result = market.get("result", "")

        return {
            "poll_time": poll_time,
            "source": self.name,
            "ticker": ticker,
            "series": series,
            "expiry_date": expiry_date,
            "result": result,
            "close_time": close_time,
            "raw_payload_hash": raw_hash,
            "schema_version": 1,
        }

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Poll all configured Kalshi weather series.

        Returns market_snapshots rows. Also populates self.pending_results
        with any newly detected market_results rows.
        """
        self._update_last_poll(now)
        self.pending_results = []
        self._pending_emitted = set()

        if not self._series:
            log.warning("KALSHI_MARKETS: KALSHI_SERIES env var is empty, nothing to poll")
            return []

        try:
            self._init_auth()
        except RuntimeError as exc:
            log.error("KALSHI_MARKETS: auth failed: %s", exc)
            return []

        snapshot_rows: list[dict] = []

        async with aiohttp.ClientSession() as session:
            for series in self._series:
                try:
                    series_snapshots, series_results = await self._poll_series(
                        session, series, now
                    )
                    snapshot_rows.extend(series_snapshots)
                    self.pending_results.extend(series_results)
                except Exception as exc:
                    log.error("KALSHI_MARKETS: error polling series %s: %s", series, exc)

        log.info(
            "KALSHI_MARKETS: %d snapshot rows, %d result rows from %d series",
            len(snapshot_rows), len(self.pending_results), len(self._series),
        )
        return snapshot_rows

    async def _poll_series(
        self, session: aiohttp.ClientSession, series: str, now: dt.datetime
    ) -> tuple[list[dict], list[dict]]:
        """
        Poll one series for open markets (snapshots) and detect closures (results).

        Returns (snapshot_rows, result_rows).
        """
        path = "/trade-api/v2/markets"
        params = {"series_ticker": series, "status": "open", "limit": 200}

        try:
            resp = await self._get_json(session, path, params)
        except Exception as exc:
            log.warning("KALSHI_MARKETS: open markets fetch failed for %s: %s", series, exc)
            return [], []

        markets = resp.get("markets", [])
        raw_hash = _compute_hash(resp)

        snapshot_rows = [
            self._market_to_snapshot_row(m, now, series, raw_hash)
            for m in markets
        ]

        current_open: set[str] = {m.get("ticker", "") for m in markets}

        # Detect closures: tickers that were open last poll but aren't now.
        prev_open = self._prev_open.get(series, set())
        disappeared = prev_open - current_open
        self._prev_open[series] = current_open

        result_rows: list[dict] = []

        # Check disappeared tickers individually.
        for ticker in disappeared:
            if ticker in self._emitted_results or ticker in self._pending_emitted:
                continue
            try:
                ticker_path = f"/trade-api/v2/markets/{ticker}"
                ticker_resp = await self._get_json(session, ticker_path)
                market = ticker_resp.get("market", {})
                if market.get("status") == "finalized":
                    ticker_hash = _compute_hash(ticker_resp)
                    result_rows.append(
                        self._market_to_result_row(market, now, series, ticker_hash)
                    )
                    self._pending_emitted.add(ticker)
            except Exception as exc:
                log.warning("KALSHI_MARKETS: failed to fetch ticker %s: %s", ticker, exc)

        # Scan recently settled markets to catch anything missed between polls.
        try:
            settled_params = {"series_ticker": series, "status": "settled", "limit": 200}
            settled_resp = await self._get_json(session, path, settled_params)
            settled_markets = settled_resp.get("markets", [])
            settled_hash = _compute_hash(settled_resp)
            for market in settled_markets:
                ticker = market.get("ticker", "")
                if ticker and ticker not in self._emitted_results and ticker not in self._pending_emitted:
                    if market.get("status") == "finalized" and market.get("result"):
                        result_rows.append(
                            self._market_to_result_row(market, now, series, settled_hash)
                        )
                        self._pending_emitted.add(ticker)
        except Exception as exc:
            log.warning("KALSHI_MARKETS: settled markets fetch failed for %s: %s", series, exc)

        return snapshot_rows, result_rows
