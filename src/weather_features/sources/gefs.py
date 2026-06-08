"""
GEFS (Global Ensemble Forecast System) source client for the weather pipeline.

Fetches GEFS 31-member ensemble forecast trajectories via Herbie.
One row per (station, valid_time, member) per poll.

Cycles: 00/06/12/18Z.
Members: 0-30 (31 total). Member 0 is the control run.
Forecast hours: 0-168 (step 3 for 0-240h; we cap at 168h).

Available: ~4-5 hours after cycle time.
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

GEFS_CYCLES = (0, 6, 12, 18)
GEFS_AVAILABILITY_DELAY_H = 5  # GEFS takes ~4-5h to become available

# Members: 0 (control) + 1-30 (perturbed) = 31 total
N_MEMBERS = 31
MEMBER_IDS = list(range(0, N_MEMBERS))

# Forecast hours: 3-hourly from 0 to 168
GEFS_FHRS = list(range(0, 169, 3))  # 0, 3, 6, ..., 168

VAR_TEMP = "TMP:2 m"


def _latest_available_cycle(now: dt.datetime) -> dt.datetime:
    """Return the most recent GEFS cycle that should be available."""
    cutoff = now - dt.timedelta(hours=GEFS_AVAILABILITY_DELAY_H)
    date = cutoff.date()
    hour = cutoff.hour
    candidates = [c for c in GEFS_CYCLES if c <= hour]
    if candidates:
        cycle_hour = max(candidates)
        return dt.datetime(date.year, date.month, date.day,
                           cycle_hour, tzinfo=dt.timezone.utc)
    prev = date - dt.timedelta(days=1)
    cycle_hour = max(GEFS_CYCLES)
    return dt.datetime(prev.year, prev.month, prev.day,
                       cycle_hour, tzinfo=dt.timezone.utc)


def _fhrs_needed(now: dt.datetime, cycle: dt.datetime) -> list[int]:
    """Return forecast hours covering next 48h from now."""
    needed_until = now + dt.timedelta(hours=48)
    result = []
    for fhr in GEFS_FHRS:
        valid = cycle + dt.timedelta(hours=fhr)
        if valid >= now - dt.timedelta(hours=3) and valid <= needed_until:
            result.append(fhr)
    return result or GEFS_FHRS[:17]  # First 48h at 3h step


def _k_to_f(k) -> Optional[float]:
    try:
        return float((k - 273.15) * 9 / 5 + 32)
    except (TypeError, ValueError):
        return None


class GEFSSource(SourceClient):
    name = "GEFS"
    table = "hourly_forecasts"
    min_poll_interval_sec = 3600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)
        self._cached_cycle: Optional[dt.datetime] = None
        # {member: {fhr: {icao: temp_k}}}
        self._cached_data: Optional[dict] = None

    def _cycle_hash(self, cycle: dt.datetime) -> str:
        return hashlib.sha256(
            f"GEFS:{cycle.isoformat()}".encode()
        ).hexdigest()

    def _fetch_member_fhr(
        self, cycle: dt.datetime, member: int, fhr: int, stations: list
    ) -> dict:
        """
        Fetch one GEFS member/fhr via Herbie and interpolate to stations.
        Returns {icao: temp_k}.
        """
        try:
            from herbie import Herbie
        except ImportError:
            raise RuntimeError("herbie not installed")

        H = Herbie(
            cycle.replace(tzinfo=None),  # Herbie expects naive UTC
            model="gefs",
            product="atmos.5",
            member=member,
            fxx=fhr,
            verbose=False,
        )

        result: dict[str, float] = {}
        try:
            ds = H.xarray(VAR_TEMP)
        except Exception as exc:
            raise ValueError(f"GEFS data not available: {exc}") from exc

        for station in stations:
            lat = station.lat
            lon = station.lon % 360  # GEFS uses 0-360
            try:
                result[station.icao] = nearest_val(ds, lat, lon)
            except Exception:
                pass

        return result

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch GEFS ensemble forecast and produce hourly_forecasts rows.
        """
        self._update_last_poll(now)

        try:
            from herbie import Herbie  # noqa: F401
        except ImportError:
            log.warning("GEFS: herbie not installed, skipping")
            return []

        cycle = _latest_available_cycle(now)
        raw_hash = self._cycle_hash(cycle)

        need_fetch = (self._cached_cycle is None or cycle > self._cached_cycle)

        if need_fetch:
            log.info("GEFS: fetching cycle %s (%d members)", cycle.isoformat(), N_MEMBERS)
            fhrs = _fhrs_needed(now, cycle)
            new_data: dict[int, dict[int, dict]] = {}  # member -> fhr -> {icao: temp_k}

            loop = asyncio.get_event_loop()
            any_success = False

            data_unavailable = False
            for member in MEMBER_IDS:
                if data_unavailable:
                    break
                new_data[member] = {}
                for fhr in fhrs:
                    try:
                        fhr_data = await loop.run_in_executor(
                            None,
                            lambda m=member, f=fhr: self._fetch_member_fhr(
                                cycle, m, f, stations
                            ),
                        )
                        new_data[member][fhr] = fhr_data
                        any_success = True
                    except ValueError as exc:
                        log.info("GEFS: m=%d fhr=%d not available: %s", member, fhr, exc)
                        if member == 0:
                            log.info("GEFS: data not yet available for cycle %s, aborting", cycle.isoformat())
                            data_unavailable = True
                        break
                    except Exception as exc:
                        log.warning("GEFS: m=%d fhr=%d failed: %s", member, fhr, exc)

            if any_success:
                self._cached_cycle = cycle
                self._cached_data = new_data
            elif self._cached_data is None:
                log.warning("GEFS: no data available, skipping poll")
                return []
            raw_hash = self._cycle_hash(self._cached_cycle)
        else:
            log.debug("GEFS: reusing cached cycle %s", self._cached_cycle.isoformat())
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

        log.info("GEFS: %d rows (cycle=%s, cached=%s)",
                 len(rows),
                 use_cycle.isoformat() if use_cycle else "none",
                 not need_fetch)
        return rows
