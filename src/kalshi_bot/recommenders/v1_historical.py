"""
TemperatureRecommenderV1Historical — same logic as V1 but reads forecast
data from the weather pipeline parquets instead of making live API calls.

Intended for backtesting: at each historical tick, call set_time(poll_time)
then recommend(snapshot) to get signals as if the V1 recommender had been
running at that moment.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from kalshi_bot.forecast import Recommender
from kalshi_bot.forecast.historical_clients import HistoricalNBMClient, HistoricalNWSClient

from .v1 import TemperatureRecommenderV1
from trading_bot.models import MarketSnapshot, Signal


class TemperatureRecommenderV1Historical(TemperatureRecommenderV1):
    """
    V1 recommender wired to historical parquet data.

    Parameters
    ----------
    data_dir:
        Root of the weather pipeline data directory (contains
        raw/daily_forecasts/source=NBM and source=NWS_FORECAST).
    All other parameters are identical to TemperatureRecommenderV1.
    """

    def __init__(self, data_dir: Path,
                 max_staleness_hours: float | None = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._nbm_hist = HistoricalNBMClient(data_dir)
        self._nws_hist = HistoricalNWSClient(data_dir)
        self._max_staleness_hours = max_staleness_hours
        # Override the live Recommender with one using historical clients.
        self._recommender = Recommender(
            nws=self._nws_hist,
            nbm=self._nbm_hist,
        )

    def set_time(self, poll_time: dt.datetime) -> None:
        """Advance the simulation clock. Call before each recommend()."""
        self._nbm_hist.set_time(poll_time, self._max_staleness_hours)
        self._nws_hist.set_time(poll_time)

    def recommend(self, snapshot: MarketSnapshot) -> list[Signal]:
        self.set_time(snapshot.ts)
        return super().recommend(snapshot)
