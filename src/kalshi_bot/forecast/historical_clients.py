"""
Historical drop-in replacements for NBMClient and NWSClient.

Instead of making live API calls, these read from the weather pipeline
parquets collected in data/raw/daily_forecasts/. They implement the same
interface so they can be injected into Recommender unchanged.

Usage:
    nbm = HistoricalNBMClient(data_dir)
    nws = HistoricalNWSClient(data_dir)
    rec = Recommender(nws=nws, nbm=nbm)

    # Before each tick in a backtest loop:
    nbm.set_time(poll_time)
    nws.set_time(poll_time)
    signals = recommender.recommend(snapshot)
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from .nbm_client import TempPercentiles
from .nws_client import DailyForecast
from .stations import STATIONS

log = logging.getLogger(__name__)

# Map ICAO → (lat, lon) for NWS lat/lon → station lookups
_ICAO_BY_LATLON: dict[tuple[float, float], str] = {
    (st.lat, st.lon): icao for icao, st in STATIONS.items()
}


class HistoricalNBMClient:
    """
    Reads NBM percentile forecasts from parquet instead of the live API.
    Call set_time(poll_time) before each use to select the right snapshot.
    """

    def __init__(self, data_dir: Path) -> None:
        path = Path(data_dir) / "raw" / "daily_forecasts" / "source=NBM"
        import pyarrow.dataset as ds
        d = ds.dataset(path, format="parquet")
        df = d.to_table().to_pandas()
        df["poll_time"] = pd.to_datetime(df["poll_time"], utc=True)
        df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
        self._df = df
        self._poll_time: Optional[dt.datetime] = None
        self._max_staleness_hours: Optional[float] = None

    def set_time(self, poll_time: dt.datetime,
                 max_staleness_hours: Optional[float] = None) -> None:
        self._poll_time = poll_time
        self._max_staleness_hours = max_staleness_hours

    def get_percentiles(self, station_icao: str,
                        station_tz_offset: int) -> list[TempPercentiles]:
        if self._poll_time is None:
            raise RuntimeError("Call set_time() before get_percentiles()")

        # Most recent poll at or before simulation time for this station
        mask = (
            (self._df["station_icao"] == station_icao) &
            (self._df["poll_time"] <= self._poll_time)
        )
        df = self._df[mask]
        if df.empty:
            return []

        latest = df["poll_time"].max()

        # Staleness filter: skip if data is too old
        if self._max_staleness_hours is not None:
            age_h = (self._poll_time - latest).total_seconds() / 3600
            if age_h > self._max_staleness_hours:
                log.debug("NBM data for %s is %.1fh old (max %.1fh) — skipping",
                          station_icao, age_h, self._max_staleness_hours)
                return []
        df = df[df["poll_time"] == latest]

        result: list[TempPercentiles] = []
        for _, row in df.iterrows():
            def _f(col: str) -> Optional[float]:
                v = row.get(col)
                return None if v is None or (isinstance(v, float) and v != v) else float(v)

            cycle = row.get("cycle")
            if pd.isnull(cycle):
                cycle = latest
            elif not isinstance(cycle, dt.datetime):
                cycle = pd.to_datetime(cycle, utc=True).to_pydatetime()

            result.append(TempPercentiles(
                station=station_icao,
                kind=str(row["kind"]),
                target_date=row["target_date"],
                cycle=cycle,
                fhr=int(row["fhr"]) if pd.notna(row.get("fhr")) else 0,
                mean=_f("mean"),
                sd=_f("sd"),
                p10=_f("p10"),
                p25=_f("p25"),
                p50=_f("p50"),
                p75=_f("p75"),
                p90=_f("p90"),
            ))
        return result


class HistoricalNWSClient:
    """
    Reads NWS daily high/low forecasts from parquet instead of the live API.
    Call set_time(poll_time) before each use.
    """

    def __init__(self, data_dir: Path) -> None:
        path = Path(data_dir) / "raw" / "daily_forecasts" / "source=NWS_FORECAST"
        import pyarrow.dataset as ds
        d = ds.dataset(path, format="parquet")
        df = d.to_table().to_pandas()
        df["poll_time"] = pd.to_datetime(df["poll_time"], utc=True)
        df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
        df["issued_at"] = pd.to_datetime(df["issued_at"], utc=True)
        self._df = df
        self._poll_time: Optional[dt.datetime] = None

    def set_time(self, poll_time: dt.datetime) -> None:
        self._poll_time = poll_time

    def get_daily_forecasts(self, lat: float, lon: float,
                            tz_standard_offset: int) -> list[DailyForecast]:
        if self._poll_time is None:
            raise RuntimeError("Call set_time() before get_daily_forecasts()")

        # Resolve lat/lon → ICAO (exact match against station registry)
        icao = _ICAO_BY_LATLON.get((lat, lon))
        if icao is None:
            # Nearest station by distance
            import math
            best, best_d = None, float("inf")
            for (slat, slon), code in _ICAO_BY_LATLON.items():
                d = math.hypot(slat - lat, slon - lon)
                if d < best_d:
                    best_d, best = d, code
            icao = best

        if icao is None:
            return []

        mask = (
            (self._df["station_icao"] == icao) &
            (self._df["poll_time"] <= self._poll_time)
        )
        df = self._df[mask]
        if df.empty:
            return []

        latest = df["poll_time"].max()
        df = df[df["poll_time"] == latest]

        # Pivot high/low into DailyForecast objects
        by_date: dict[dt.date, dict] = {}
        for _, row in df.iterrows():
            d = row["target_date"]
            by_date.setdefault(d, {})
            if row["kind"] == "high":
                by_date[d]["high_f"] = float(row["value_f"]) if pd.notna(row["value_f"]) else None
            else:
                by_date[d]["low_f"] = float(row["value_f"]) if pd.notna(row["value_f"]) else None

        issued = df["issued_at"].iloc[0].to_pydatetime() if not df.empty else latest
        return [
            DailyForecast(
                date=d,
                high_f=vals.get("high_f"),
                low_f=vals.get("low_f"),
                issued_at=issued,
            )
            for d, vals in sorted(by_date.items())
        ]
