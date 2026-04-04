"""
Weather strategy for NYC temperature markets.

For each KXTEMPNYCH ticker, fetches the Open-Meteo point forecast and models
the probability that the actual temperature exceeds the threshold as:

  P(actual >= threshold) = 1 - Φ((threshold - forecast) / FORECAST_STD_F)

where Φ is the standard normal CDF and FORECAST_STD_F is an assumed forecast
error standard deviation. A value of 2.0°F is conservative for same-day
forecasts; you may want to increase it for markets settling further out.
"""

import logging
import math

from src.data.weather import WeatherFeed
from src.kalshi.models import EdgeEstimate, Market
from src.kalshi.ticker_parser import parse_nyc_temp_ticker
from src.strategies.base import Strategy

logger = logging.getLogger(__name__)

FORECAST_STD_F = 2.0   # assumed 1-sigma forecast error in °F
CONFIDENCE = 0.5       # half-Kelly — conservative until strategy is validated


def _normal_cdf(x: float) -> float:
    """Standard normal CDF using math.erf (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _prob_above(forecast_f: float, threshold_f: float, std_f: float = FORECAST_STD_F) -> float:
    """P(actual >= threshold) given a normally-distributed forecast."""
    z = (threshold_f - forecast_f) / std_f
    return 1.0 - _normal_cdf(z)


class WeatherStrategy(Strategy):
    def __init__(self):
        self._feed = WeatherFeed()

    def estimate_edge(self, markets: dict[str, Market]) -> list[EdgeEstimate]:
        estimates = []
        for ticker, market in markets.items():
            parsed = parse_nyc_temp_ticker(ticker)
            if parsed is None:
                continue

            forecast = self._feed.fetch_nyc_temperature_f(parsed.dt)
            if forecast is None:
                logger.warning("[%s] No forecast available — skipping", ticker)
                continue

            prob = _prob_above(forecast, parsed.threshold_f)
            logger.info(
                "[%s] forecast=%.1f°F threshold=%.2f°F p(YES)=%.3f",
                ticker, forecast, parsed.threshold_f, prob,
            )
            estimates.append(EdgeEstimate(
                ticker=ticker,
                yes_probability=prob,
                confidence=CONFIDENCE,
            ))

        return estimates
