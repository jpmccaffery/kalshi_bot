"""
ECMWF IFS deterministic (open data) source client for the weather pipeline.

Fetches ECMWF IFS hourly forecast trajectories via Herbie (AWS S3 mirror).
ECMWF open data is fully free since October 2025 — no auth required.

Cycles: 00/06/12/18Z, available ~7 hours after cycle.
Forecast hours: 0-90 at 1h, then 3h steps to 144h.
We cover the next 48h from the current time.

Produces hourly_forecasts rows: one row per (station, valid_time) per poll.
Member = 0 (deterministic).

Bilinear interpolation to each station's lat/lon via xarray.interp().
Cache: reuse data for the same cycle rather than re-downloading.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from typing import Optional

from .base import SourceClient, nearest_val

log = logging.getLogger(__name__)

# Cycles: 00/06/12/18 UTC, available ~7h after cycle time
ECMWF_CYCLES = (0, 6, 12, 18)
ECMWF_AVAILABILITY_DELAY_H = 7

# ECMWF oper publishes at 3-hourly steps only
ECMWF_FHRS = list(range(0, 145, 3))

# GRIB variable search strings for Herbie
VAR_TEMP = ":2 m temperature:"     # 2m temperature (K)
VAR_DEW = ":2 m dewpoint:"          # 2m dewpoint (K)
VAR_U10 = ":10 m u-component of wind:"
VAR_V10 = ":10 m v-component of wind:"


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
    """Kelvin to Fahrenheit."""
    try:
        return float((k - 273.15) * 9 / 5 + 32)
    except (TypeError, ValueError):
        return None


def _ms_to_mph(ms) -> Optional[float]:
    """m/s to mph."""
    try:
        return float(ms * 2.23694)
    except (TypeError, ValueError):
        return None


def _wind_dir(u, v) -> Optional[float]:
    """Meteorological wind direction (degrees) from U/V components."""
    try:
        import math
        deg = (270 - math.degrees(math.atan2(v, u))) % 360
        return float(deg)
    except Exception:
        return None


class ECMWFDetSource(SourceClient):
    name = "ECMWF_DET"
    table = "hourly_forecasts"
    min_poll_interval_sec = 3600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)
        self._cached_cycle: Optional[dt.datetime] = None
        # Cached data: {fhr: {icao: {"temp_k": ..., "dew_k": ..., "u10": ..., "v10": ...}}}
        self._cached_data: Optional[dict] = None

    def _cycle_hash(self, cycle: dt.datetime) -> str:
        return hashlib.sha256(
            f"ECMWF_DET:{cycle.isoformat()}".encode()
        ).hexdigest()

    def _fhrs_needed(self, now: dt.datetime, cycle: dt.datetime) -> list[int]:
        """Return forecast hours that cover the next 48h from now."""
        needed_until = now + dt.timedelta(hours=48)
        result = []
        for fhr in ECMWF_FHRS:
            valid = cycle + dt.timedelta(hours=fhr)
            if valid >= now - dt.timedelta(hours=1) and valid <= needed_until:
                result.append(fhr)
        return result or ECMWF_FHRS[:24]  # Fallback: first 24 hours

    def _fetch_fhr(self, cycle: dt.datetime, fhr: int, stations: list) -> dict:
        """
        Fetch one forecast hour from ECMWF via Herbie and interpolate to stations.
        Returns {icao: {"temp_k": float, "dew_k": float, "u10": float, "v10": float}}.
        """
        try:
            from herbie import Herbie
            import cfgrib
            import glob as _glob
            import warnings as _warnings
        except ImportError:
            raise RuntimeError("herbie/cfgrib not installed")

        H = Herbie(cycle.replace(tzinfo=None), model="ifs", product="oper", fxx=fhr, verbose=False)
        if H.grib is None:
            raise ValueError(f"Data not available for cycle {cycle} fhr={fhr}")

        # Herbie's subset extraction doesn't work for ECMWF's index format.
        # Download the full GRIB and open it with cfgrib directly.
        # Delete after extraction — each file is ~140MB and accumulates fast.
        import os as _os
        H.download()
        date_str = cycle.strftime("%Y%m%d")
        pattern = f"{H.save_dir}/ifs/{date_str}/*.grib2"
        local_files = _glob.glob(pattern)
        if not local_files:
            raise ValueError(f"Downloaded GRIB not found at {pattern}")

        grib_path = local_files[0]
        try:
            with _warnings.catch_warnings():
                _warnings.simplefilter("ignore")
                datasets = cfgrib.open_datasets(grib_path)
            # ECMWF oper dataset [2] contains t2m, d2m; dataset [1] has u10, v10
            ds_t = next((ds for ds in datasets if "t2m" in ds.data_vars), None)
            ds_d = next((ds for ds in datasets if "d2m" in ds.data_vars), None)
            ds_u = next((ds for ds in datasets if "u10" in ds.data_vars), None)
            ds_v = next((ds for ds in datasets if "v10" in ds.data_vars), None)

            # Eagerly load arrays before deleting — cfgrib is lazy
            t2m_vals = ds_t["t2m"].values if ds_t is not None else None
            d2m_vals = ds_d["d2m"].values if ds_d is not None else None
            u10_vals = ds_u["u10"].values if ds_u is not None else None
            v10_vals = ds_v["v10"].values if ds_v is not None else None
            lat_arr  = ds_t.latitude.values if ds_t is not None else None
            lon_arr  = ds_t.longitude.values if ds_t is not None else None
        finally:
            _os.unlink(grib_path)

        result: dict[str, dict] = {}

        if ds_t is None:
            raise ValueError(f"No t2m variable found in ECMWF GRIB for fhr={fhr}")

        import numpy as _np
        for station in stations:
            lat = station.lat
            lon = station.lon  # ECMWF oper uses -180 to 180

            try:
                iy = int(_np.argmin(_np.abs(lat_arr - lat)))
                ix = int(_np.argmin(_np.abs(lon_arr - lon)))
                temp_k = float(t2m_vals[iy, ix])
            except Exception as exc:
                log.debug("ECMWF_DET: temp lookup failed for %s: %s", station.icao, exc)
                continue

            dew_k: Optional[float] = None
            if d2m_vals is not None:
                try:
                    dew_k = float(d2m_vals[iy, ix])
                except Exception:
                    pass

            u10: Optional[float] = None
            v10: Optional[float] = None
            if u10_vals is not None:
                try:
                    u10 = float(u10_vals[iy, ix])
                except Exception:
                    pass
            if v10_vals is not None:
                try:
                    v10 = float(v10_vals[iy, ix])
                except Exception:
                    pass

            result[station.icao] = {
                "temp_k": temp_k,
                "dew_k": dew_k,
                "u10": u10,
                "v10": v10,
            }

        return result

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch ECMWF IFS deterministic forecast and produce hourly_forecasts rows.
        """
        self._update_last_poll(now)

        try:
            from herbie import Herbie  # noqa: F401
        except ImportError:
            log.warning("ECMWF_DET: herbie not installed, skipping")
            return []

        cycle = _latest_available_cycle(now)
        raw_hash = self._cycle_hash(cycle)

        # Determine if we need to fetch new data
        need_fetch = (self._cached_cycle is None or cycle > self._cached_cycle)

        if need_fetch:
            log.info("ECMWF_DET: fetching cycle %s", cycle.isoformat())
            fhrs = self._fhrs_needed(now, cycle)
            new_data: dict[int, dict] = {}

            loop = asyncio.get_event_loop()
            for fhr in fhrs:
                try:
                    fhr_data = await loop.run_in_executor(
                        None, lambda f=fhr: self._fetch_fhr(cycle, f, stations)
                    )
                    new_data[fhr] = fhr_data
                except Exception as exc:
                    if "not available" in str(exc).lower() or isinstance(exc, ValueError):
                        log.info("ECMWF_DET: cycle %s fhr=%d not yet available, aborting",
                                 cycle.isoformat(), fhr)
                        break  # If first fhr isn't available, none will be
                    else:
                        log.warning("ECMWF_DET: fhr=%d failed: %s", fhr, exc)

            if new_data:
                self._cached_cycle = cycle
                self._cached_data = new_data
            elif self._cached_data is None:
                log.warning("ECMWF_DET: no data available, skipping poll")
                return []
            # else: use old cached data, update hash to old cycle's hash
            raw_hash = self._cycle_hash(self._cached_cycle)
        else:
            log.debug("ECMWF_DET: reusing cached cycle %s", self._cached_cycle.isoformat())
            if self._cached_data is None:
                return []

        # Build rows from cached data
        rows: list[dict] = []
        use_cycle = self._cached_cycle

        for fhr, fhr_data in (self._cached_data or {}).items():
            valid_time = use_cycle + dt.timedelta(hours=fhr)
            for station in stations:
                sdata = fhr_data.get(station.icao)
                if not sdata:
                    continue

                temp_k = sdata.get("temp_k")
                dew_k = sdata.get("dew_k")
                u10 = sdata.get("u10")
                v10 = sdata.get("v10")

                temp_f = _k_to_f(temp_k)
                dewpoint_f = _k_to_f(dew_k)

                wind_speed_ms: Optional[float] = None
                wind_dir_deg: Optional[float] = None
                if u10 is not None and v10 is not None:
                    import math
                    wind_speed_ms = math.sqrt(u10**2 + v10**2)
                    wind_dir_deg = _wind_dir(u10, v10)
                wind_mph = _ms_to_mph(wind_speed_ms) if wind_speed_ms is not None else None

                rows.append({
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
                    "sky_cover_pct": None,
                    "precip_in": None,
                    "issued_at": use_cycle,
                    "cycle": use_cycle,
                    "fhr": fhr,
                    "raw_payload_hash": raw_hash,
                    "schema_version": 1,
                })

        log.info("ECMWF_DET: %d rows (cycle=%s, cached=%s)",
                 len(rows),
                 use_cycle.isoformat() if use_cycle else "none",
                 not need_fetch)
        return rows
