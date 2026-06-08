"""
GFS MOS source client for the weather features pipeline.

Fetches daily min/max temperature MOS forecasts from the Iowa State
Mesonet API (IEM) for each station.

API: https://mesonet.agron.iastate.edu/api/1/mos.json?station=<ICAO>&model=GFS

Produces daily_forecasts rows: one row per (station, target_date, kind) per poll.
Percentile columns are null (MOS provides point forecasts only).
cycle from runtime field; fhr from hours between runtime and ftime.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from typing import Optional

import aiohttp

from .base import SourceClient

log = logging.getLogger(__name__)

IEM_MOS_URL = "https://mesonet.agron.iastate.edu/api/1/mos.json"
USER_AGENT = "kalshi-weather-pipeline/1.0 jpmccaffery@gmail.com"


def _parse_utc(s: str) -> Optional[dt.datetime]:
    """Parse a UTC datetime string like '2026-05-19 06:00'."""
    if not s:
        return None
    try:
        # IEM returns strings like "2026-05-19 06:00"
        naive = dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        try:
            # Fallback: ISO format
            naive = dt.datetime.fromisoformat(s.strip())
            if naive.tzinfo is None:
                return naive.replace(tzinfo=dt.timezone.utc)
            return naive.astimezone(dt.timezone.utc)
        except ValueError:
            return None


def _compute_hash(data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


class GFSMOSSource(SourceClient):
    name = "GFS_MOS"
    table = "daily_forecasts"
    min_poll_interval_sec = 600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch GFS MOS daily forecasts for all stations via IEM API.

        Returns daily_forecasts rows with null percentile columns.
        """
        self._update_last_poll(now)
        rows: list[dict] = []

        headers = {"User-Agent": USER_AGENT}

        async def fetch_station(station, session: aiohttp.ClientSession):
            params = {"station": station.icao, "model": "GFS"}
            url = IEM_MOS_URL
            try:
                if self._semaphore:
                    async with self._semaphore:
                        async with session.get(url, params=params, headers=headers) as resp:
                            resp.raise_for_status()
                            data = await resp.json(content_type=None)
                else:
                    async with session.get(url, params=params, headers=headers) as resp:
                        resp.raise_for_status()
                        data = await resp.json(content_type=None)
            except Exception as exc:
                log.warning("GFS_MOS fetch failed for %s: %s", station.icao, exc)
                return []

            raw_hash = _compute_hash(data)
            entries = data.get("data", [])
            if not entries:
                return []

            station_rows = []
            for entry in entries:
                runtime_str = entry.get("runtime") or entry.get("model_run")
                ftime_str = entry.get("ftime")
                if not runtime_str or not ftime_str:
                    continue

                cycle = _parse_utc(runtime_str)
                ftime = _parse_utc(ftime_str)
                if cycle is None or ftime is None:
                    continue

                fhr = int((ftime - cycle).total_seconds() / 3600)

                # IEM GFS MOS encodes both high and low in n_x:
                #   ftime hour == 0  → daily HIGH for the period ending at ftime
                #                      (afternoon max; target_date = ftime.date() - 1 day)
                #   ftime hour == 12 → daily LOW (overnight min ending at ftime)
                #                      (target_date = ftime.date())
                # n_n never appears in practice for this API endpoint.
                raw_val = entry.get("n_x")
                if raw_val is None:
                    continue
                try:
                    value_f = float(raw_val)
                except (ValueError, TypeError):
                    continue

                if ftime.hour == 0:
                    kind = "high"
                    target_date = (ftime - dt.timedelta(days=1)).date()
                elif ftime.hour == 12:
                    kind = "low"
                    target_date = ftime.date()
                else:
                    continue  # not a daily summary row

                station_rows.append({
                    "poll_time": now,
                    "source": self.name,
                    "station_icao": station.icao,
                    "city": station.market_city,
                    "target_date": target_date,
                    "kind": kind,
                    "value_f": value_f,
                    "p10": None,
                    "p25": None,
                    "p50": None,
                    "p75": None,
                    "p90": None,
                    "mean": None,
                    "sd": None,
                    "issued_at": cycle,
                    "cycle": cycle,
                    "fhr": fhr,
                    "raw_payload_hash": raw_hash,
                    "schema_version": 1,
                })

            return station_rows

        async with aiohttp.ClientSession() as session:
            tasks = [fetch_station(s, session) for s in stations]
            results = await asyncio.gather(*tasks)

        for station_rows in results:
            rows.extend(station_rows)

        log.info("GFS_MOS: %d rows from %d stations", len(rows), len(stations))
        return rows
