"""
NBM NBP (probabilistic) text bulletin source client for the weather pipeline.

Wraps the existing kalshi_bot.forecast.nbm_client.NBMClient (synchronous).
Runs the sync client in a thread executor.

Cycle caching: NBP bulletins cycle at 01Z, 07Z, 13Z, 19Z and become available
~90 minutes after the cycle hour. We compute the latest expected cycle and only
re-fetch when a new cycle should be available. But we always write rows every
poll using cached content — the identical hash is the signal nothing changed.

Produces daily_forecasts rows: one row per (station, target_date, kind) per poll.
All percentile columns are filled. value_f = p50.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from typing import Optional

from kalshi_bot.forecast.nbm_client import NBMClient, TempPercentiles

from .base import SourceClient

log = logging.getLogger(__name__)

# NBP full-coverage cycles (UTC hours).
NBM_FULL_CYCLES = (1, 7, 13, 19)
# A new cycle becomes available ~90 minutes after the cycle hour.
NBM_AVAILABILITY_DELAY_HOURS = 1.5


def _latest_available_cycle(now: dt.datetime) -> dt.datetime:
    """
    Compute the most recent NBP cycle timestamp that should be available.

    A cycle at hour H on date D is available at approximately (D H:00Z + 90min).
    Returns a timezone-aware UTC datetime for the cycle itself (not the
    availability time).
    """
    cutoff = now - dt.timedelta(hours=NBM_AVAILABILITY_DELAY_HOURS)
    date = cutoff.date()
    hour = cutoff.hour

    candidates = [c for c in NBM_FULL_CYCLES if c <= hour]
    if candidates:
        cycle_hour = max(candidates)
        return dt.datetime(date.year, date.month, date.day,
                           cycle_hour, tzinfo=dt.timezone.utc)

    # Wrap to previous day's last cycle.
    prev = date - dt.timedelta(days=1)
    cycle_hour = max(NBM_FULL_CYCLES)
    return dt.datetime(prev.year, prev.month, prev.day,
                       cycle_hour, tzinfo=dt.timezone.utc)


class NBMSource(SourceClient):
    name = "NBM"
    table = "daily_forecasts"
    min_poll_interval_sec = 600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)
        self._client = NBMClient()
        # Cache: cycle_dt -> (raw_hash, list[TempPercentiles] per station)
        # Key: cycle datetime; value: (hash_str, {icao: list[TempPercentiles]})
        self._cache: dict[dt.datetime, tuple[str, dict[str, list[TempPercentiles]]]] = {}
        self._current_cycle: Optional[dt.datetime] = None

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch NBM percentile forecasts for all stations.

        Uses in-memory cycle caching: only fetches a new bulletin when the
        expected cycle timestamp advances. Always writes rows every poll.
        """
        self._update_last_poll(now)

        expected_cycle = _latest_available_cycle(now)

        # Determine if we need to fetch a new bulletin.
        need_fetch = (self._current_cycle is None or
                      expected_cycle > self._current_cycle)

        if need_fetch:
            log.info("NBM: fetching new bulletin for cycle %s", expected_cycle.isoformat())
            cycle_date = expected_cycle.date()
            cycle_hour = expected_cycle.hour

            async def fetch_all_stations():
                loop = asyncio.get_event_loop()
                station_data: dict[str, list[TempPercentiles]] = {}
                bulletin_text: Optional[str] = None

                # Fetch the bulletin once (it covers all stations).
                try:
                    if self._semaphore:
                        async with self._semaphore:
                            bulletin_text = await loop.run_in_executor(
                                None,
                                lambda: self._client.fetch_bulletin(cycle_date, cycle_hour),
                            )
                    else:
                        bulletin_text = await loop.run_in_executor(
                            None,
                            lambda: self._client.fetch_bulletin(cycle_date, cycle_hour),
                        )
                except Exception as exc:
                    log.warning("NBM: bulletin fetch failed for cycle %s: %s",
                                expected_cycle.isoformat(), exc)
                    return None, {}

                raw_hash = hashlib.sha256(bulletin_text.encode()).hexdigest()

                # Parse per station.
                for station in stations:
                    try:
                        percentiles = await loop.run_in_executor(
                            None,
                            lambda s=station: self._client.get_percentiles(
                                s.icao,
                                date=cycle_date,
                                cycle=cycle_hour,
                                station_tz_offset=s.tz_standard_offset,
                            ),
                        )
                        station_data[station.icao] = percentiles
                    except Exception as exc:
                        log.warning("NBM: parse failed for %s: %s", station.icao, exc)
                        station_data[station.icao] = []

                return raw_hash, station_data

            raw_hash, station_data = await fetch_all_stations()

            if raw_hash is not None:
                self._cache[expected_cycle] = (raw_hash, station_data)
                self._current_cycle = expected_cycle
            elif self._current_cycle is None:
                # No cache at all yet — can't produce rows.
                log.warning("NBM: no cached data available, skipping poll")
                return []
            # else: fall through to use the old cache

        # Use cached data (either just fetched or from a previous cycle).
        use_cycle = self._current_cycle
        if use_cycle not in self._cache:
            log.warning("NBM: cache miss for cycle %s", use_cycle)
            return []

        raw_hash, station_data = self._cache[use_cycle]

        rows: list[dict] = []
        for station in stations:
            percentiles = station_data.get(station.icao, [])
            for tp in percentiles:
                cycle_ts = tp.cycle
                if cycle_ts.tzinfo is None:
                    cycle_ts = cycle_ts.replace(tzinfo=dt.timezone.utc)

                rows.append({
                    "poll_time": now,
                    "source": self.name,
                    "station_icao": station.icao,
                    "city": station.market_city,
                    "target_date": tp.target_date,
                    "kind": tp.kind,
                    "value_f": float(tp.p50) if tp.p50 is not None else None,
                    "p10": float(tp.p10) if tp.p10 is not None else None,
                    "p25": float(tp.p25) if tp.p25 is not None else None,
                    "p50": float(tp.p50) if tp.p50 is not None else None,
                    "p75": float(tp.p75) if tp.p75 is not None else None,
                    "p90": float(tp.p90) if tp.p90 is not None else None,
                    "mean": float(tp.mean) if tp.mean is not None else None,
                    "sd": float(tp.sd) if tp.sd is not None else None,
                    "issued_at": cycle_ts,
                    "cycle": cycle_ts,
                    "fhr": int(tp.fhr) if tp.fhr is not None else None,
                    "raw_payload_hash": raw_hash,
                    "schema_version": 1,
                })

        log.info("NBM: %d rows from %d stations (cycle=%s, cached=%s)",
                 len(rows), len(stations), use_cycle.isoformat(),
                 not need_fetch)
        return rows
