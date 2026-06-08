"""
Abstract base class for all weather pipeline source clients.

Every source must implement poll(), which always returns a list of row dicts
(possibly empty) and never raises. The scheduler calls poll() concurrently
with an asyncio.Semaphore to limit total concurrent HTTP requests.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np


def nearest_val(ds, lat: float, lon: float) -> float:
    """
    Extract the value of the first data variable in `ds` at the nearest
    grid point to (lat, lon).

    Works for both 1-D lat/lon dimension coords (GEFS, ECMWF) and 2-D
    lat/lon coordinate arrays on a native projection grid (HRRR).
    No scipy required.
    """
    lat_arr = ds.latitude.values
    lon_arr = ds.longitude.values
    var = list(ds.data_vars)[0]

    if lat_arr.ndim == 1:
        iy = int(np.argmin(np.abs(lat_arr - lat)))
        ix = int(np.argmin(np.abs(lon_arr - lon)))
        return float(ds[var].values[iy, ix])
    else:
        # 2-D grid (native projection like HRRR Lambert conformal)
        dist = (lat_arr - lat) ** 2 + (lon_arr - lon) ** 2
        iy, ix = np.unravel_index(int(np.argmin(dist)), dist.shape)
        return float(ds[var].values[iy, ix])


class SourceClient(ABC):
    """
    Base class for all data source clients.

    Subclasses must set class-level attributes:
        name: str           — unique short name (e.g. "NWS_FORECAST")
        table: str          — target table name
        min_poll_interval_sec: int  — minimum seconds between polls

    The scheduler respects min_poll_interval_sec to avoid over-polling slow
    sources (e.g. CLI doesn't need polling every 10 minutes).
    """

    name: str
    table: str  # "hourly_forecasts" | "daily_forecasts" | "observations" | "market_snapshots" | "market_results"
    min_poll_interval_sec: int = 600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        self._semaphore = semaphore
        self._last_poll_at: Optional[dt.datetime] = None

    @property
    def last_poll_at(self) -> Optional[dt.datetime]:
        return self._last_poll_at

    def _update_last_poll(self, now: dt.datetime) -> None:
        self._last_poll_at = now

    @abstractmethod
    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch data for all stations and return a list of row dicts.

        Contract:
        - Always returns a list (possibly empty).
        - Never raises — catch and log internally.
        - Each row must conform to the table's schema.
        - The poll_time field must equal `now`.

        Args:
            now: UTC datetime of this poll cycle.
            stations: list of Station objects to poll.

        Returns:
            List of row dicts ready for storage.write().
        """
        ...
