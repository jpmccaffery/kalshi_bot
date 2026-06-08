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


def _candidate_cycles(now: dt.datetime) -> list[dt.datetime]:
    """Return cycle datetimes from newest to oldest, covering ~24h."""
    result = []
    date = now.date()
    hour = now.hour
    for _ in range(2):
        for c in sorted([c for c in NBM_FULL_CYCLES if c <= hour], reverse=True):
            result.append(dt.datetime(date.year, date.month, date.day,
                                      c, tzinfo=dt.timezone.utc))
        date -= dt.timedelta(days=1)
        hour = 23
    return result


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

        candidates = _candidate_cycles(now)
        newest_cycle = candidates[0] if candidates else None

        # Determine if we need to fetch a new bulletin.
        need_fetch = (self._current_cycle is None or
                      (newest_cycle is not None and newest_cycle > self._current_cycle))

        if need_fetch:
            async def fetch_all_stations():
                loop = asyncio.get_event_loop()

                # Try candidates newest-first until one succeeds.
                bulletin_text: Optional[str] = None
                fetched_cycle: Optional[dt.datetime] = None
                for candidate in candidates:
                    if candidate <= (self._current_cycle or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
                        break  # no point trying cycles we already have
                    c_date, c_hour = candidate.date(), candidate.hour
                    try:
                        if self._semaphore:
                            async with self._semaphore:
                                bulletin_text = await loop.run_in_executor(
                                    None,
                                    lambda d=c_date, h=c_hour: self._client.fetch_bulletin(d, h),
                                )
                        else:
                            bulletin_text = await loop.run_in_executor(
                                None,
                                lambda d=c_date, h=c_hour: self._client.fetch_bulletin(d, h),
                            )
                        fetched_cycle = candidate
                        log.info("NBM: fetched bulletin for cycle %s", candidate.isoformat())
                        break
                    except Exception as exc:
                        log.info("NBM: cycle %s not yet available (%s), trying previous",
                                 candidate.isoformat(), exc)

                if bulletin_text is None or fetched_cycle is None:
                    log.warning("NBM: no bulletin available for any recent cycle")
                    return None, None, {}

                raw_hash = hashlib.sha256(bulletin_text.encode()).hexdigest()
                station_data: dict[str, list[TempPercentiles]] = {}
                c_date, c_hour = fetched_cycle.date(), fetched_cycle.hour

                # Parse per station.
                for station in stations:
                    try:
                        percentiles = await loop.run_in_executor(
                            None,
                            lambda s=station: self._client.get_percentiles(
                                s.icao,
                                date=c_date,
                                cycle=c_hour,
                                station_tz_offset=s.tz_standard_offset,
                            ),
                        )
                        station_data[station.icao] = percentiles
                    except Exception as exc:
                        log.warning("NBM: parse failed for %s: %s", station.icao, exc)
                        station_data[station.icao] = []

                return raw_hash, fetched_cycle, station_data

            raw_hash, fetched_cycle, station_data = await fetch_all_stations()

            if raw_hash is not None:
                self._cache[fetched_cycle] = (raw_hash, station_data)
                self._current_cycle = fetched_cycle
            elif self._current_cycle is None:
                # No cache at all yet — can't produce rows.
                log.warning("NBM: no cached data available, skipping poll")
                return []
            # else: fall through to use the old cache

        # Use cached data (either just fetched or from a previous cycle).
        if not need_fetch:
            log.info("NBM: using cached cycle %s", self._current_cycle.isoformat() if self._current_cycle else "none")
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
