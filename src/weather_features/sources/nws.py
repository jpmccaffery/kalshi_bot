"""
NWS api.weather.gov source client for the weather features pipeline.

Wraps the existing kalshi_bot.forecast.nws_client.NWSClient (synchronous).
Runs the sync client in a thread executor so it doesn't block the event loop.

Produces daily_forecasts rows: one row per (station, target_date, kind) per poll.
Percentile columns are null (NWS provides point forecasts only).
issued_at from the response's updateTime. cycle and fhr are null.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from typing import Optional

from kalshi_bot.forecast.nws_client import NWSClient

from .base import SourceClient

log = logging.getLogger(__name__)

NWS_USER_AGENT = "kalshi-weather-pipeline/1.0 jpmccaffery@gmail.com"


class NWSForecastSource(SourceClient):
    name = "NWS_FORECAST"
    table = "daily_forecasts"
    min_poll_interval_sec = 600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)
        self._client = NWSClient(user_agent=NWS_USER_AGENT)

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch NWS daily forecasts for all stations.

        Runs the synchronous NWSClient in a thread executor for each station.
        Returns daily_forecasts rows with null percentile columns.
        """
        self._update_last_poll(now)
        rows: list[dict] = []

        async def fetch_station(station):
            loop = asyncio.get_event_loop()
            try:
                if self._semaphore:
                    async with self._semaphore:
                        forecasts = await loop.run_in_executor(
                            None,
                            lambda: self._client.get_daily_forecasts(
                                station.lat, station.lon, station.tz_standard_offset
                            ),
                        )
                else:
                    forecasts = await loop.run_in_executor(
                        None,
                        lambda: self._client.get_daily_forecasts(
                            station.lat, station.lon, station.tz_standard_offset
                        ),
                    )
            except Exception as exc:
                log.warning("NWS fetch failed for %s: %s", station.icao, exc)
                return []

            if not forecasts:
                return []

            # Build a hash from the response content for provenance.
            # We hash the serialized forecast data since we don't have raw JSON.
            hash_payload = json.dumps(
                [
                    {
                        "date": str(f.date),
                        "high_f": f.high_f,
                        "low_f": f.low_f,
                        "issued_at": f.issued_at.isoformat(),
                    }
                    for f in forecasts
                ],
                sort_keys=True,
            )
            raw_hash = hashlib.sha256(hash_payload.encode()).hexdigest()

            station_rows = []
            for forecast in forecasts:
                issued_at = forecast.issued_at
                if issued_at.tzinfo is None:
                    issued_at = issued_at.replace(tzinfo=dt.timezone.utc)

                if forecast.high_f is not None:
                    station_rows.append({
                        "poll_time": now,
                        "source": self.name,
                        "station_icao": station.icao,
                        "city": station.market_city,
                        "target_date": forecast.date,
                        "kind": "high",
                        "value_f": float(forecast.high_f),
                        "p10": None,
                        "p25": None,
                        "p50": None,
                        "p75": None,
                        "p90": None,
                        "mean": None,
                        "sd": None,
                        "issued_at": issued_at,
                        "cycle": None,
                        "fhr": None,
                        "raw_payload_hash": raw_hash,
                        "schema_version": 1,
                    })

                if forecast.low_f is not None:
                    station_rows.append({
                        "poll_time": now,
                        "source": self.name,
                        "station_icao": station.icao,
                        "city": station.market_city,
                        "target_date": forecast.date,
                        "kind": "low",
                        "value_f": float(forecast.low_f),
                        "p10": None,
                        "p25": None,
                        "p50": None,
                        "p75": None,
                        "p90": None,
                        "mean": None,
                        "sd": None,
                        "issued_at": issued_at,
                        "cycle": None,
                        "fhr": None,
                        "raw_payload_hash": raw_hash,
                        "schema_version": 1,
                    })

            return station_rows

        tasks = [fetch_station(s) for s in stations]
        results = await asyncio.gather(*tasks)
        for station_rows in results:
            rows.extend(station_rows)

        log.info("NWS_FORECAST: %d rows from %d stations", len(rows), len(stations))
        return rows
