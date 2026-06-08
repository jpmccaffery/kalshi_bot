"""
HRRR (High-Resolution Rapid Refresh) source client for the weather pipeline.

Fetches HRRR 3km CONUS hourly forecast trajectories via Herbie.
One row per (station, valid_time) per poll. Member = 0 (deterministic).

Cycles: every hour UTC.
  - 00/06/12/18Z cycles: forecast hours 0-48.
  - All other hours: forecast hours 0-18.
Available: ~45 minutes after cycle time.

HRRR only covers CONUS — all 20 stations are within its domain.
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

# Extended cycles (go to F48)
HRRR_EXTENDED_CYCLES = {0, 6, 12, 18}

VAR_TEMP = "TMP:2 m"
VAR_DEW = "DPT:2 m"
VAR_UGRD = "UGRD:10 m"
VAR_VGRD = "VGRD:10 m"
VAR_PRES = "PRES:surface"


def _candidate_cycles(now: dt.datetime, n: int = 4) -> list[dt.datetime]:
    """Return the n most recent HRRR cycle datetimes, newest first."""
    base = now.replace(minute=0, second=0, microsecond=0)
    return [base - dt.timedelta(hours=i) for i in range(n)]


def _max_fhr(cycle_hour: int) -> int:
    """Max forecast hour for a given HRRR cycle hour."""
    return 48 if cycle_hour in HRRR_EXTENDED_CYCLES else 18


def _fhrs_needed(now: dt.datetime, cycle: dt.datetime) -> list[int]:
    """Return forecast hours covering the next 48h from now."""
    max_fhr = _max_fhr(cycle.hour)
    needed_until = now + dt.timedelta(hours=48)
    result = []
    for fhr in range(0, max_fhr + 1):
        valid = cycle + dt.timedelta(hours=fhr)
        if valid >= now - dt.timedelta(hours=1) and valid <= needed_until:
            result.append(fhr)
    return result or list(range(0, min(19, max_fhr + 1)))


def _k_to_f(k) -> Optional[float]:
    try:
        return float((k - 273.15) * 9 / 5 + 32)
    except (TypeError, ValueError):
        return None


def _ms_to_mph(ms) -> Optional[float]:
    try:
        return float(ms * 2.23694)
    except (TypeError, ValueError):
        return None


def _wind_dir(u, v) -> Optional[float]:
    try:
        deg = (270 - math.degrees(math.atan2(v, u))) % 360
        return float(deg)
    except Exception:
        return None


class HRRRSource(SourceClient):
    name = "HRRR"
    table = "hourly_forecasts"
    min_poll_interval_sec = 1800

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)
        self._cached_cycle: Optional[dt.datetime] = None
        # {fhr: {icao: {"temp_k", "dew_k", "u10", "v10", "pres_pa"}}}
        self._cached_data: Optional[dict] = None

    def _cycle_hash(self, cycle: dt.datetime) -> str:
        return hashlib.sha256(
            f"HRRR:{cycle.isoformat()}".encode()
        ).hexdigest()

    def _fetch_fhr(
        self, cycle: dt.datetime, fhr: int, stations: list
    ) -> dict:
        """
        Fetch one HRRR forecast hour via Herbie and interpolate to stations.
        Returns {icao: {"temp_k", "dew_k", "u10", "v10", "pres_pa"}}.
        """
        try:
            from herbie import Herbie
        except ImportError:
            raise RuntimeError("herbie not installed")

        H = Herbie(cycle.replace(tzinfo=None), model="hrrr", product="sfc", fxx=fhr, verbose=False)

        result: dict[str, dict] = {}

        # Temperature
        try:
            ds_t = H.xarray(VAR_TEMP)
        except Exception as exc:
            raise ValueError(f"HRRR temp not available: {exc}") from exc

        # Optional variables — don't fail if unavailable
        ds_d, ds_u, ds_v, ds_p = None, None, None, None
        for var_str, attr in [(VAR_DEW, "ds_d"), (VAR_UGRD, "ds_u"),
                               (VAR_VGRD, "ds_v"), (VAR_PRES, "ds_p")]:
            try:
                data = H.xarray(var_str)
                if attr == "ds_d":
                    ds_d = data
                elif attr == "ds_u":
                    ds_u = data
                elif attr == "ds_v":
                    ds_v = data
                elif attr == "ds_p":
                    ds_p = data
            except Exception:
                pass

        for station in stations:
            lat = station.lat
            lon = station.lon % 360  # HRRR grid uses 0-360

            try:
                temp_k = nearest_val(ds_t, lat, lon)
            except Exception as exc:
                log.debug("HRRR: temp lookup failed for %s: %s", station.icao, exc)
                continue

            dew_k: Optional[float] = None
            if ds_d is not None:
                try:
                    dew_k = nearest_val(ds_d, lat, lon)
                except Exception:
                    pass

            u10: Optional[float] = None
            v10: Optional[float] = None
            if ds_u is not None:
                try:
                    u10 = nearest_val(ds_u, lat, lon)
                except Exception:
                    pass
            if ds_v is not None:
                try:
                    v10 = nearest_val(ds_v, lat, lon)
                except Exception:
                    pass

            pres_pa: Optional[float] = None
            if ds_p is not None:
                try:
                    pres_pa = nearest_val(ds_p, lat, lon)
                except Exception:
                    pass

            result[station.icao] = {
                "temp_k": temp_k,
                "dew_k": dew_k,
                "u10": u10,
                "v10": v10,
                "pres_pa": pres_pa,
            }

        return result

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch HRRR forecast and produce hourly_forecasts rows.
        """
        self._update_last_poll(now)

        try:
            from herbie import Herbie  # noqa: F401
        except ImportError:
            log.warning("HRRR: herbie not installed, skipping")
            return []

        candidates = _candidate_cycles(now)
        newest = candidates[0]

        need_fetch = (self._cached_cycle is None or newest > self._cached_cycle)

        if need_fetch:
            loop = asyncio.get_event_loop()
            for cycle in candidates:
                if cycle <= (self._cached_cycle or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
                    break  # no point trying cycles we already have
                log.info("HRRR: trying cycle %s (max_fhr=%d)",
                         cycle.isoformat(), _max_fhr(cycle.hour))
                fhrs = _fhrs_needed(now, cycle)
                new_data: dict[int, dict] = {}
                available = True

                for fhr in fhrs:
                    try:
                        fhr_data = await loop.run_in_executor(
                            None, lambda f=fhr, c=cycle: self._fetch_fhr(c, f, stations)
                        )
                        new_data[fhr] = fhr_data
                    except ValueError as exc:
                        log.info("HRRR: cycle %s fhr=%d not yet available, trying previous cycle: %s",
                                 cycle.isoformat(), fhr, exc)
                        available = False
                        break
                    except Exception as exc:
                        log.warning("HRRR: fhr=%d failed: %s", fhr, exc)

                if new_data and available:
                    self._cached_cycle = cycle
                    self._cached_data = new_data
                    break
                elif new_data:
                    # Got partial data but cycle not fully available — try previous
                    continue

            if self._cached_data is None:
                log.warning("HRRR: no data available from any recent cycle, skipping poll")
                return []
        else:
            log.info("HRRR: using cached cycle %s", self._cached_cycle.isoformat())
            if self._cached_data is None:
                return []

        raw_hash = self._cycle_hash(self._cached_cycle)

        # Build rows
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
                pres_pa = sdata.get("pres_pa")

                temp_f = _k_to_f(temp_k)
                dewpoint_f = _k_to_f(dew_k)

                wind_speed_ms: Optional[float] = None
                wind_dir_deg: Optional[float] = None
                if u10 is not None and v10 is not None:
                    wind_speed_ms = math.sqrt(u10**2 + v10**2)
                    wind_dir_deg = _wind_dir(u10, v10)
                wind_mph = _ms_to_mph(wind_speed_ms) if wind_speed_ms is not None else None
                pressure_mb = (pres_pa / 100.0) if pres_pa is not None else None

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
                    "pressure_mb": float(pressure_mb) if pressure_mb is not None else None,
                    "sky_cover_pct": None,
                    "precip_in": None,
                    "issued_at": use_cycle,
                    "cycle": use_cycle,
                    "fhr": fhr,
                    "raw_payload_hash": raw_hash,
                    "schema_version": 1,
                })

        log.info("HRRR: %d rows (cycle=%s, cached=%s)",
                 len(rows),
                 use_cycle.isoformat() if use_cycle else "none",
                 not need_fetch)
        return rows
