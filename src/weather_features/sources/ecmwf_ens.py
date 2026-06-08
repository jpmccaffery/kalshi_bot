"""
ECMWF IFS ensemble (ENFO) source client for the weather pipeline.

Fetches ECMWF IFS 51-member ensemble forecast trajectories via Herbie.
One row per (station, valid_time, member) per poll.

Cycles: 00/06/12/18Z, available ~7h after cycle.
Members: 0-50 (51 total). Member 0 is the control run.

IMPORTANT: This source produces a very large number of rows:
  51 members × 20 stations × ~48 forecast hours = ~49,000 rows per poll.
Cache the data per cycle to avoid redundant downloads.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import math
from typing import Optional

from .base import SourceClient, nearest_val

log = logging.getLogger(__name__)

# Cycles and availability same as deterministic
ECMWF_CYCLES = (0, 6, 12, 18)
ECMWF_AVAILABILITY_DELAY_H = 7

# Ensemble members: 0 (control) + 1-50 (perturbed) = 51 total
N_MEMBERS = 51
MEMBER_IDS = list(range(0, N_MEMBERS))

# ECMWF ENFO publishes at 3-hourly steps only
ECMWF_FHRS = list(range(0, 145, 3))

VAR_TEMP = ":2 m temperature:"


def _latest_available_cycle(now: dt.datetime) -> dt.datetime:
    """Return the most recent ECMWF cycle that should be available."""
    cutoff = now - dt.timedelta(hours=ECMWF_AVAILABILITY_DELAY_H)
    date = cutoff.date()
    hour = cutoff.hour
    candidates = [c for c in ECMWF_CYCLES if c <= hour]
    if candidates:
        cycle_hour = max(candidates)
        return dt.datetime(date.year, date.month, date.day,
                           cycle_hour, tzinfo=dt.timezone.utc)
    prev = date - dt.timedelta(days=1)
    cycle_hour = max(ECMWF_CYCLES)
    return dt.datetime(prev.year, prev.month, prev.day,
                       cycle_hour, tzinfo=dt.timezone.utc)


def _k_to_f(k) -> Optional[float]:
    try:
        return float((k - 273.15) * 9 / 5 + 32)
    except (TypeError, ValueError):
        return None


class ECMWFEnsSource(SourceClient):
    name = "ECMWF_ENS"
    table = "hourly_forecasts"
    min_poll_interval_sec = 3600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)
        self._cached_cycle: Optional[dt.datetime] = None
        # {member: {fhr: {icao: temp_k}}}
        self._cached_data: Optional[dict] = None

    def _cycle_hash(self, cycle: dt.datetime) -> str:
        return hashlib.sha256(
            f"ECMWF_ENS:{cycle.isoformat()}".encode()
        ).hexdigest()

    def _fhrs_needed(self, now: dt.datetime, cycle: dt.datetime) -> list[int]:
        """Return forecast hours covering next 48h from now."""
        needed_until = now + dt.timedelta(hours=48)
        result = []
        for fhr in ECMWF_FHRS:
            valid = cycle + dt.timedelta(hours=fhr)
            if valid >= now - dt.timedelta(hours=1) and valid <= needed_until:
                result.append(fhr)
        return result or ECMWF_FHRS[:24]

    def _fetch_fhr(self, cycle: dt.datetime, fhr: int, stations: list) -> dict:
        """
        Fetch one forecast hour from ECMWF ENFO using byte-range requests.
        Returns {member: {icao: temp_k}} for all perturbed members (1-50).

        Downloads only the 2t messages (~32MB) from the index rather than
        the full GRIB (~6.5GB). The control run (member 0) is skipped as it
        requires a separate file.
        """
        try:
            import cfgrib
            import json as _json
            import os as _os
            import tempfile as _tempfile
            import warnings as _warnings
            import numpy as _np
            import requests as _requests
        except ImportError:
            raise RuntimeError("cfgrib/requests not installed")

        base = (f"https://storage.googleapis.com/ecmwf-open-data/"
                f"{cycle.strftime('%Y%m%d')}/{cycle.strftime('%H')}z/ifs/0p25/enfo/"
                f"{cycle.strftime('%Y%m%d%H%M%S')}-{fhr}h-enfo-ef")
        grib_url = base + ".grib2"
        idx_url  = base + ".index"

        # Check availability via index
        idx_resp = _requests.get(idx_url, timeout=15)
        if idx_resp.status_code != 200:
            raise ValueError(f"ENFO index not available: HTTP {idx_resp.status_code}")

        entries = [_json.loads(ln) for ln in idx_resp.text.strip().splitlines() if ln.strip()]
        t2m_entries = [e for e in entries if e.get("param") == "2t"]
        if not t2m_entries:
            raise ValueError(f"No 2t entries in ENFO index for fhr={fhr}")

        # Fetch t2m bytes in parallel via range requests
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

        def _fetch_chunk(e):
            start, length = e["_offset"], e["_length"]
            r = _requests.get(grib_url,
                              headers={"Range": f"bytes={start}-{start+length-1}"},
                              timeout=30)
            r.raise_for_status()
            return (e["_offset"], r.content)

        chunks_by_offset = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_chunk, e): e for e in t2m_entries}
            for future in _as_completed(futures):
                offset, content = future.result()
                chunks_by_offset[offset] = content

        chunks = [chunks_by_offset[e["_offset"]] for e in t2m_entries]

        # Write concatenated messages to temp file and open with cfgrib.
        # Load all data eagerly before deleting the file — cfgrib is lazy.
        tmp = _tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
        try:
            for chunk in chunks:
                tmp.write(chunk)
            tmp.flush()
            tmp.close()

            with _warnings.catch_warnings():
                _warnings.simplefilter("ignore")
                datasets = cfgrib.open_datasets(tmp.name)
            ds_t = next((ds for ds in datasets if "t2m" in ds.data_vars), None)
            if ds_t is None:
                raise ValueError(f"No t2m in downloaded ENFO data for fhr={fhr}")

            lat_arr  = ds_t.latitude.values
            lon_arr  = ds_t.longitude.values
            t2m_all  = ds_t["t2m"].values      # (50, 721, 1440) — load now
            members  = ds_t["number"].values
        finally:
            _os.unlink(tmp.name)

        # Extract per-station temperatures for all members
        result: dict[int, dict[str, float]] = {}
        for station in stations:
            iy = int(_np.argmin(_np.abs(lat_arr - station.lat)))
            ix = int(_np.argmin(_np.abs(lon_arr - station.lon)))
            for m, t in zip(members, t2m_all[:, iy, ix]):
                result.setdefault(int(m), {})[station.icao] = float(t)

        log.debug("ECMWF_ENS: fhr=%d fetched %d members, %.1f MB",
                  fhr, len(result), sum(len(c) for c in chunks) / 1024 / 1024)
        return result

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch ECMWF ensemble forecast and produce hourly_forecasts rows.
        Downloads once per fhr (all members in one GRIB file).
        """
        self._update_last_poll(now)

        try:
            from herbie import Herbie  # noqa: F401
        except ImportError:
            log.warning("ECMWF_ENS: herbie not installed, skipping")
            return []

        cycle = _latest_available_cycle(now)
        raw_hash = self._cycle_hash(cycle)

        need_fetch = (self._cached_cycle is None or cycle > self._cached_cycle)

        if need_fetch:
            log.info("ECMWF_ENS: fetching cycle %s (%d members, one download per fhr)",
                     cycle.isoformat(), N_MEMBERS)
            fhrs = self._fhrs_needed(now, cycle)
            # new_data: {member: {fhr: {icao: temp_k}}}
            new_data: dict[int, dict[int, dict]] = {m: {} for m in MEMBER_IDS}

            loop = asyncio.get_event_loop()
            any_success = False

            for fhr in fhrs:
                try:
                    fhr_result = await loop.run_in_executor(
                        None, lambda f=fhr: self._fetch_fhr(cycle, f, stations)
                    )
                    # fhr_result: {member: {icao: temp_k}}
                    for member, station_data in fhr_result.items():
                        if member in new_data:
                            new_data[member][fhr] = station_data
                    any_success = True
                    log.info("ECMWF_ENS: fhr=%d fetched %d members", fhr, len(fhr_result))
                except ValueError as exc:
                    log.info("ECMWF_ENS: fhr=%d not available, aborting: %s", fhr, exc)
                    break
                except Exception as exc:
                    log.warning("ECMWF_ENS: fhr=%d failed: %s", fhr, exc)

            if any_success:
                self._cached_cycle = cycle
                self._cached_data = new_data
            elif self._cached_data is None:
                log.warning("ECMWF_ENS: no data available, skipping poll")
                return []
            raw_hash = self._cycle_hash(self._cached_cycle)
        else:
            log.debug("ECMWF_ENS: reusing cached cycle %s", self._cached_cycle.isoformat())
            if self._cached_data is None:
                return []

        # Build rows
        rows: list[dict] = []
        use_cycle = self._cached_cycle

        for member, member_data in (self._cached_data or {}).items():
            for fhr, fhr_data in member_data.items():
                valid_time = use_cycle + dt.timedelta(hours=fhr)
                for station in stations:
                    temp_k = fhr_data.get(station.icao)
                    if temp_k is None:
                        continue
                    temp_f = _k_to_f(temp_k)

                    rows.append({
                        "poll_time": now,
                        "source": self.name,
                        "station_icao": station.icao,
                        "city": station.market_city,
                        "valid_time": valid_time,
                        "member": member,
                        "temp_f": float(temp_f) if temp_f is not None else None,
                        "dewpoint_f": None,
                        "wind_mph": None,
                        "wind_dir_deg": None,
                        "pressure_mb": None,
                        "sky_cover_pct": None,
                        "precip_in": None,
                        "issued_at": use_cycle,
                        "cycle": use_cycle,
                        "fhr": fhr,
                        "raw_payload_hash": raw_hash,
                        "schema_version": 1,
                    })

        log.info("ECMWF_ENS: %d rows (cycle=%s, cached=%s)",
                 len(rows),
                 use_cycle.isoformat() if use_cycle else "none",
                 not need_fetch)
        return rows
