"""
Kalshi recommender implementations.

ProbMeanReversionRecommender
    Buys YES contracts when the ask price dips significantly below the
    rolling mean ask — expecting the probability to revert upward.

    Uses yes_ask as the signal price.  Edge is proportional to how far
    the current ask has fallen below the rolling mean.

AtmCheapBuyerRecommender
    For each series+expiry group, finds the single contract whose yes_ask
    is closest to 0.50 (at-the-money).  Signals long if that price is
    below a configurable ceiling (default 0.48).

    This avoids trading deep-out-of-the-money longshots while limiting
    exposure to at most one contract per series per expiry per tick.

    Ticker format assumed: {SERIES}-{EXPIRY}-{STRIKE}
    e.g. KXINX-26APR14H1600-B7087  →  series=KXINX, expiry=26APR14H1600
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from trading_bot.models import DataSchema, MarketSnapshot, Signal

logger = logging.getLogger(__name__)

# Splits e.g. "KXINX-26APR14H1600-B7087" into ("KXINX", "26APR14H1600", "B7087")
_TICKER_RE = re.compile(r"^(.+?)-(\d{2}[A-Z]{3}\d{2}[^-]*)-(.+)$")


class ProbMeanReversionRecommender:
    """
    Generate a long signal when yes_ask is significantly below its rolling mean.

    Parameters
    ----------
    window:
        Rolling window in bars for computing the mean (default 20).
    threshold:
        Minimum fractional dip required to signal.
        E.g. 0.05 → signal only when yes_ask < mean * (1 - 0.05).
    horizon:
        Expected holding period per signal (used for Signal.horizon).
    """

    def __init__(
        self,
        window: int    = 20,
        threshold: float = 0.05,
        horizon: timedelta = timedelta(days=7),
    ) -> None:
        self._window    = window
        self._threshold = threshold
        self._horizon   = horizon

    @property
    def required_schema(self) -> DataSchema:
        return DataSchema(columns={"yes_ask": "float64"})

    def recommend(self, snapshot: MarketSnapshot) -> list[Signal]:
        signals: list[Signal] = []

        for mkt in snapshot.bars["symbol"].tolist():
            hist = (
                snapshot.history[snapshot.history["symbol"] == mkt]
                .sort_values("ts")
            )
            if len(hist) < self._window:
                logger.debug(
                    "%s: only %d bars, need %d — skipping", mkt, len(hist), self._window
                )
                continue

            asks        = hist["yes_ask"].values
            mean_ask    = float(asks[-self._window:].mean())
            current_ask = float(asks[-1])

            if mean_ask <= 0 or current_ask <= 0:
                continue

            dip = (mean_ask - current_ask) / mean_ask
            if dip < self._threshold:
                continue

            # Edge: how far below mean, scaled and capped
            edge = min(dip * 3, 0.30)

            signals.append(Signal(
                symbol      = mkt,
                direction   = "long",
                edge        = edge,
                edge_lower  = edge * 0.5,
                edge_upper  = edge * 1.5,
                horizon     = self._horizon,
                conviction  = min(dip * 10, 1.0),
                generated_at= snapshot.ts,
                metadata    = {
                    "yes_ask":     round(current_ask, 4),
                    "rolling_mean": round(mean_ask, 4),
                    "dip_pct":      round(dip * 100, 2),
                },
            ))
            logger.debug(
                "%s: yes_ask=%.4f  mean=%.4f  dip=%.2f%%  edge=%.4f",
                mkt, current_ask, mean_ask, dip * 100, edge,
            )

        return sorted(signals, key=lambda s: s.edge, reverse=True)


class AtmCheapBuyerRecommender:
    """
    Buy the at-the-money contract for each series+expiry when it is cheap.

    For each group of tickers sharing the same series and expiry date, picks
    the single contract whose yes_ask is closest to 0.50.  Signals long if
    that price is below ``ceiling`` (default 0.48).

    Parameters
    ----------
    ceiling:
        Maximum yes_ask to consider "cheap enough" to buy (default 0.48).
        Contracts at or above this price are skipped.
    horizon:
        Expected holding period passed through to the Signal.
    """

    def __init__(
        self,
        ceiling: float     = 0.48,
        horizon: timedelta = timedelta(hours=24),
    ) -> None:
        self._ceiling = ceiling
        self._horizon = horizon

    @property
    def required_schema(self) -> DataSchema:
        return DataSchema(columns={"yes_ask": "float64"})

    def recommend(self, snapshot: MarketSnapshot) -> list[Signal]:
        bars = snapshot.bars

        # Parse each ticker into (series, expiry, ticker)
        groups: dict[tuple[str, str], list[tuple[str, float]]] = {}
        for _, row in bars.iterrows():
            ticker  = row["symbol"]
            yes_ask = float(row["yes_ask"])
            if yes_ask <= 0 or yes_ask != yes_ask:   # skip zero / NaN
                logger.debug("Ticker %r has yes_ask=%.4f (zero/NaN) — skipping", ticker, yes_ask)
                continue
            m = _TICKER_RE.match(ticker)
            if not m:
                logger.debug("Ticker %r doesn't match expected format — skipping", ticker)
                continue
            series, expiry = m.group(1), m.group(2)
            groups.setdefault((series, expiry), []).append((ticker, yes_ask))

        signals: list[Signal] = []
        for (series, expiry), candidates in groups.items():
            # Pick the contract closest to 0.50
            atm_ticker, atm_ask = min(candidates, key=lambda x: abs(x[1] - 0.50))

            if atm_ask >= self._ceiling:
                logger.debug(
                    "%s: ATM ask %.4f >= ceiling %.4f — no signal",
                    atm_ticker, atm_ask, self._ceiling,
                )
                continue

            edge = round(self._ceiling - atm_ask, 4)   # how far below ceiling
            signals.append(Signal(
                symbol      = atm_ticker,
                direction   = "long",
                edge        = edge,
                edge_lower  = edge * 0.5,
                edge_upper  = edge * 1.5,
                horizon     = self._horizon,
                conviction  = min(edge * 5, 1.0),
                generated_at= snapshot.ts,
                metadata    = {
                    "series":   series,
                    "expiry":   expiry,
                    "yes_ask":  atm_ask,
                    "ceiling":  self._ceiling,
                },
            ))
            logger.debug(
                "%s: ATM ask=%.4f  edge=%.4f  (series=%s expiry=%s)",
                atm_ticker, atm_ask, edge, series, expiry,
            )

        return sorted(signals, key=lambda s: s.edge, reverse=True)
