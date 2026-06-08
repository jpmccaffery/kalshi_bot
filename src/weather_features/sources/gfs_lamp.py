"""
GFS LAMP source client for the weather features pipeline.

Fetches hourly temperature/weather LAV (LAMP) forecasts from the Iowa State
Mesonet API (IEM) for each station.

API: https://mesonet.agron.iastate.edu/api/1/mos.json?station=<ICAO>&model=LAV

Produces hourly_forecasts rows: one row per (station, valid_time) per poll.
Member = 0 (LAMP is deterministic).

Field notes from IEM LAMP:
  - tmp:  2m temperature (°F)
  - dpt:  dewpoint (°F)
  - wsp:  wind speed — IEM LAMP returns knots, multiply by 1.15078 to get mph
  - wdr:  wind direction (degrees)
  - skc:  sky cover in oktas (0-8), multiply by 12.5 to get percent
  - p06 / p01:  precipitation probability (%)
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

# Conversion factors
KNOTS_TO_MPH = 1.15078
OKTAS_TO_PCT = 12.5

# Sky cover code → percent coverage
SKY_CODE_MAP = {
    "CLR": 0.0,
    "SKC": 0.0,
    "FEW": 12.5,
    "SCT": 37.5,
    "BKN": 62.5,
    "OVC": 100.0,
    "VV": 100.0,  # Vertical visibility (obscured)
}


def _parse_utc(s: str) -> Optional[dt.datetime]:
    """Parse a UTC datetime string like '2026-05-19 06:00'."""
    if not s:
        return None
    try:
        naive = dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        try:
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


def _safe_float(val) -> Optional[float]:
    """Convert to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _sky_cover_pct(entry: dict) -> Optional[float]:
    """Extract sky cover percent from a LAMP entry.

    IEM LAMP may provide 'skc' as an oktas integer (0-8) or as a string code.
    Try numeric first, then fall back to string codes.
    """
    skc = entry.get("skc")
    if skc is None:
        return None

    # Try numeric oktas
    try:
        oktas = float(skc)
        return min(100.0, oktas * OKTAS_TO_PCT)
    except (ValueError, TypeError):
        pass

    # Try string code
    code = str(skc).strip().upper()
    return SKY_CODE_MAP.get(code)


class GFSLAMPSource(SourceClient):
    name = "GFS_LAMP"
    table = "hourly_forecasts"
    min_poll_interval_sec = 600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch GFS LAMP hourly forecasts for all stations via IEM API.

        Returns hourly_forecasts rows with member=0 (deterministic).
        """
        self._update_last_poll(now)
        rows: list[dict] = []

        headers = {"User-Agent": USER_AGENT}

        async def fetch_station(station, session: aiohttp.ClientSession):
            params = {"station": station.icao, "model": "LAV"}
            try:
                if self._semaphore:
                    async with self._semaphore:
                        async with session.get(
                            IEM_MOS_URL, params=params, headers=headers
                        ) as resp:
                            resp.raise_for_status()
                            data = await resp.json(content_type=None)
                else:
                    async with session.get(
                        IEM_MOS_URL, params=params, headers=headers
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json(content_type=None)
            except Exception as exc:
                log.warning("GFS_LAMP fetch failed for %s: %s", station.icao, exc)
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
                valid_time = _parse_utc(ftime_str)
                if cycle is None or valid_time is None:
                    continue

                fhr = int((valid_time - cycle).total_seconds() / 3600)

                # Temperature
                temp_f = _safe_float(entry.get("tmp"))

                # Dewpoint
                dewpoint_f = _safe_float(entry.get("dpt"))

                # Wind speed: knots → mph
                wsp_raw = _safe_float(entry.get("wsp"))
                wind_mph = (wsp_raw * KNOTS_TO_MPH) if wsp_raw is not None else None

                # Wind direction
                wind_dir_deg = _safe_float(entry.get("wdr"))

                # Sky cover
                sky_cover_pct = _sky_cover_pct(entry)

                # Precip probability (use p06 first, then p01)
                precip_prob = _safe_float(entry.get("p06")) or _safe_float(entry.get("p01"))
                # Store as fraction (0-1) would be lossy — keep as 0-100 percent
                # but schema has precip_in (inches), not probability. Store None.
                # LAMP doesn't give precip accumulation, only probability.
                precip_in = None

                station_rows.append({
                    "poll_time": now,
                    "source": self.name,
                    "station_icao": station.icao,
                    "city": station.market_city,
                    "valid_time": valid_time,
                    "member": 0,
                    "temp_f": float(temp_f) if temp_f is not None else None,
                    "dewpoint_f": float(dewpoint_f) if dewpoint_f is not None else None,
                    "wind_mph": float(wind_mph) if wind_mph is not None else None,
                    "wind_dir_deg": float(wind_dir_deg) if wind_dir_deg is not None else None,
                    "pressure_mb": None,
                    "sky_cover_pct": float(sky_cover_pct) if sky_cover_pct is not None else None,
                    "precip_in": precip_in,
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

        log.info("GFS_LAMP: %d rows from %d stations", len(rows), len(stations))
        return rows
