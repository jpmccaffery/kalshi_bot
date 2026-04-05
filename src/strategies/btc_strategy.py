"""
BTC strategy for daily price markets (KXBTCD series).

Each market asks: "Will Bitcoin be above $X at time T?"

Models P(BTC > threshold at expiry) using a lognormal random walk:

  z = (ln(threshold/spot) - mu*T) / (sigma * sqrt(T))
  P(YES) = 1 - Φ(z)

where:
  spot      = current BTC price (CoinGecko free API, no key required)
  T         = years until market close_time
  mu        = annualised drift (0.0 — neutral, no directional view)
  sigma     = annualised volatility (1.0 — BTC is volatile, conservative)

Confidence is set low (0.25) until the model is validated in demo.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import requests

from src.kalshi.models import EdgeEstimate, Market
from src.strategies.base import Strategy

logger = logging.getLogger(__name__)

ANNUAL_DRIFT = 0.0
ANNUAL_VOL = 1.0        # ~100% annualised vol — conservative for BTC
CONFIDENCE = 0.25       # quarter-Kelly until validated
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


def _fetch_btc_spot() -> Optional[float]:
    try:
        resp = requests.get(
            COINGECKO_URL,
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["bitcoin"]["usd"])
    except Exception as e:
        logger.error("Failed to fetch BTC spot price: %s", e)
        return None


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _prob_above_at_expiry(spot: float, threshold: float, years: float) -> float:
    if years <= 0:
        return 1.0 if spot >= threshold else 0.0
    z = (math.log(threshold / spot) - ANNUAL_DRIFT * years) / (ANNUAL_VOL * math.sqrt(years))
    return 1.0 - _normal_cdf(z)


def _parse_threshold(ticker: str) -> Optional[float]:
    # KXBTCD-26APR0417-T67499.99  →  67499.99
    try:
        return float(ticker.split("-T")[-1])
    except (ValueError, IndexError):
        return None


class BtcStrategy(Strategy):
    def estimate_edge(self, markets: dict[str, Market]) -> list[EdgeEstimate]:
        btc_markets = {t: m for t, m in markets.items() if t.startswith("KXBTCD")}
        if not btc_markets:
            return []

        spot = _fetch_btc_spot()
        if spot is None:
            return []
        logger.info("BTC spot: $%.2f", spot)

        now = datetime.now(timezone.utc)
        estimates = []

        for ticker, market in btc_markets.items():
            threshold = _parse_threshold(ticker)
            if threshold is None:
                logger.warning("[%s] Could not parse threshold", ticker)
                continue

            if not market.close_time:
                logger.warning("[%s] No close_time", ticker)
                continue

            deadline = datetime.fromisoformat(market.close_time.replace("Z", "+00:00"))
            years = max((deadline - now).total_seconds() / (365.25 * 24 * 3600), 0)

            prob = _prob_above_at_expiry(spot, threshold, years)
            logger.info(
                "[%s] spot=$%.0f threshold=$%.0f expiry=%.3f yrs p(YES)=%.3f",
                ticker, spot, threshold, years, prob,
            )
            estimates.append(EdgeEstimate(
                ticker=ticker,
                yes_probability=prob,
                confidence=CONFIDENCE,
            ))

        return estimates
