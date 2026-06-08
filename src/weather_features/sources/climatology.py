"""
CLIMO (1991-2020 Daily Normals) source client for the weather features pipeline.

One-time pull from NCEI. On first poll, downloads all 20 stations and writes to
data/static/climatology/. Subsequent polls are no-ops if the partition exists.

Source: https://www.ncei.noaa.gov/data/normals-daily/1991-2020/access/<ID>.csv

Produces climatology rows: one row per (station, month, day).
Table: "climatology" (written to data/static/climatology/).
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import logging
from pathlib import Path
from typing import Optional

import aiohttp

from .base import SourceClient

log = logging.getLogger(__name__)

NCEI_BASE = "https://www.ncei.noaa.gov/data/normals-daily/1991-2020/access"
USER_AGENT = "kalshi-weather-pipeline/1.0 jpmccaffery@gmail.com"

# Map station ICAO → NCEI station ID
NCEI_IDS = {
    "KNYC": "USW00094728",
    "KMIA": "USW00012839",
    "KPHL": "USW00013739",
    "KBOS": "USW00014739",
    "KATL": "USW00013874",
    "KDCA": "USW00013743",
    "KORD": "USW00094846",
    "KHOU": "USW00012918",
    "KDFW": "USW00003927",
    "KAUS": "USW00013904",
    "KSAT": "USW00012921",
    "KMSY": "USW00012916",
    "KMSP": "USW00014922",
    "KOKC": "USW00013967",
    "KDEN": "USW00003017",
    "KPHX": "USW00023183",
    "KLAX": "USW00023174",
    "KSFO": "USW00023234",
    "KSEA": "USW00024233",
    "KLAS": "USW00023169",
}

SOURCE_DATASET = "NCEI-1991-2020-Normals"


def _safe_float(val: str) -> Optional[float]:
    """Parse a CSV value to float, returning None for missing/flags."""
    if not val or val.strip() in ("", "-9999", "-9999.0", "M", "S", "T"):
        return None
    # NCEI normals sometimes have quality flags appended (e.g. "68.4C")
    cleaned = val.strip().rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_ncei_csv(csv_text: str, station_icao: str) -> list[dict]:
    """
    Parse NCEI daily normals CSV into climatology rows.

    Expected columns include:
      DATE,DLY-TMAX-NORMAL,DLY-TMIN-NORMAL,DLY-TMAX-ALLTIMERCD,DLY-TMIN-ALLTIMERCD
    plus many others we ignore.
    """
    rows = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for record in reader:
        date_str = record.get("DATE", "").strip()
        if not date_str:
            continue

        # DATE is in MM-DD format
        try:
            parts = date_str.split("-")
            if len(parts) != 2:
                continue
            month = int(parts[0])
            day = int(parts[1])
        except (ValueError, IndexError):
            continue

        # Normal high/low (°F)
        normal_high_f = _safe_float(record.get("DLY-TMAX-NORMAL", ""))
        normal_low_f  = _safe_float(record.get("DLY-TMIN-NORMAL", ""))

        # Standard deviation of daily high/low (°F) — measure of day-to-day variability
        stddev_high_f = _safe_float(record.get("DLY-TMAX-STDDEV", ""))
        stddev_low_f  = _safe_float(record.get("DLY-TMIN-STDDEV", ""))

        rows.append({
            "station_icao":  station_icao,
            "month":         month,
            "day":           day,
            "normal_high_f": float(normal_high_f) if normal_high_f is not None else None,
            "normal_low_f":  float(normal_low_f)  if normal_low_f  is not None else None,
            "stddev_high_f": float(stddev_high_f) if stddev_high_f is not None else None,
            "stddev_low_f":  float(stddev_low_f)  if stddev_low_f  is not None else None,
            "source_dataset": SOURCE_DATASET,
            "schema_version": 1,
        })

    return rows


class CLIMOSource(SourceClient):
    """
    One-time climatology downloader.

    Checks if data/static/climatology/part-*.parquet exists. If yes, returns
    empty list immediately. If no, downloads all stations and returns rows.
    """
    name = "CLIMO"
    table = "climatology"
    min_poll_interval_sec = 600  # Usually a no-op after first run

    def __init__(
        self,
        semaphore: Optional[asyncio.Semaphore] = None,
        data_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(semaphore)
        self._data_dir = data_dir
        self._done = False  # In-memory flag: skip once we've written

    def _climo_exists(self) -> bool:
        """Check if any parquet file exists in the climatology partition."""
        if self._data_dir is None:
            return False
        climo_dir = self._data_dir / "static" / "climatology"
        if not climo_dir.exists():
            return False
        parquet_files = list(climo_dir.glob("part-*.parquet"))
        return len(parquet_files) > 0

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch 1991-2020 NCEI normals for all stations (one-time).

        Returns empty list on subsequent calls if data already exists.
        """
        self._update_last_poll(now)

        # Skip if already done in this process run
        if self._done:
            return []

        # Skip if parquet already written to disk
        if self._climo_exists():
            log.info("CLIMO: data already exists, skipping download")
            self._done = True
            return []

        log.info("CLIMO: downloading 1991-2020 normals for %d stations", len(stations))
        rows: list[dict] = []

        headers = {"User-Agent": USER_AGENT}

        async def fetch_station(station, session: aiohttp.ClientSession):
            ncei_id = NCEI_IDS.get(station.icao)
            if not ncei_id:
                log.warning("CLIMO: no NCEI ID for station %s, skipping", station.icao)
                return []

            url = f"{NCEI_BASE}/{ncei_id}.csv"
            try:
                if self._semaphore:
                    async with self._semaphore:
                        async with session.get(url, headers=headers) as resp:
                            resp.raise_for_status()
                            csv_text = await resp.text()
                else:
                    async with session.get(url, headers=headers) as resp:
                        resp.raise_for_status()
                        csv_text = await resp.text()
            except Exception as exc:
                log.warning("CLIMO: download failed for %s (%s): %s",
                            station.icao, ncei_id, exc)
                return []

            try:
                return _parse_ncei_csv(csv_text, station.icao)
            except Exception as exc:
                log.warning("CLIMO: parse failed for %s: %s", station.icao, exc)
                return []

        async with aiohttp.ClientSession() as session:
            tasks = [fetch_station(s, session) for s in stations]
            results = await asyncio.gather(*tasks)

        for station_rows in results:
            rows.extend(station_rows)

        if rows:
            self._done = True
            log.info("CLIMO: downloaded %d rows for %d stations", len(rows), len(stations))
        else:
            log.warning("CLIMO: no rows returned — will retry next poll")

        return rows
