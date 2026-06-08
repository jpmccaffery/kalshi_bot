"""
Async poll loop for the weather features pipeline.

Aligns polls to 10-minute wall-clock boundaries (0, 10, 20, 30, 40, 50 past
the hour). Uses asyncio.Semaphore(8) to limit concurrent HTTP requests.

Clean shutdown: catches SIGINT and SIGTERM, flushes storage, writes final
health log before exiting.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal
import sys
from typing import Optional

from tqdm import tqdm

from . import health, storage
from .sources.base import SourceClient

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 600
SEMAPHORE_LIMIT = 8


def _next_poll_time(now: dt.datetime, interval_sec: int = POLL_INTERVAL_SEC) -> dt.datetime:
    """
    Compute the next poll time aligned to interval_sec boundaries.

    For a 600s (10-minute) interval, aligns to 0, 10, 20, 30, 40, 50 past
    the hour based on epoch time.
    """
    epoch = now.timestamp()
    next_epoch = (epoch // interval_sec + 1) * interval_sec
    return dt.datetime.fromtimestamp(next_epoch, tz=dt.timezone.utc)


async def tick(
    now: dt.datetime,
    sources: list[SourceClient],
    stations: list,
    csv_rows: Optional[dict[str, list[dict]]] = None,
) -> None:
    """
    Run one poll cycle: poll all eligible sources concurrently.

    Args:
        now: UTC datetime of this poll cycle.
        sources: list of SourceClient instances to poll.
        stations: list of Station objects to pass to each source.
        csv_rows: if not None, accumulate rows here keyed by table name
                  (used for --csv mode).
    """
    eligible = [
        s for s in sources
        if (s.last_poll_at is None or
            (now - s.last_poll_at).total_seconds() >= s.min_poll_interval_sec)
    ]

    if not eligible:
        log.debug("tick: no eligible sources at %s", now.isoformat())
        return

    log.info("tick: polling %d/%d sources at %s",
             len(eligible), len(sources), now.isoformat())

    # Record attempts before polling.
    for source in eligible:
        health.record_attempt(source.name, now)

    async def run_source(source: SourceClient):
        try:
            rows = await source.poll(now, stations)
        except Exception as exc:
            log.error("source=%s unhandled error: %s", source.name, exc)
            health.record_failure(source.name, now, exc)
            return

        if isinstance(rows, Exception):
            health.record_failure(source.name, now, rows)
            return

        # Collect unique hashes for health tracking.
        hashes: set[str] = set()
        for row in rows:
            h = row.get("raw_payload_hash")
            if h:
                hashes.add(h)

        if rows:
            try:
                storage.write(source.table, source.name, rows, now)
            except Exception as exc:
                log.error("storage.write failed for source=%s: %s", source.name, exc)
                health.record_failure(source.name, now, exc)
                return

            if csv_rows is not None:
                csv_rows.setdefault(source.table, []).extend(rows)

        health.record_success(source.name, now, len(rows), hashes)

        # Handle market_results side-channel from KalshiMarketsSource.
        from .sources.kalshi_markets import KalshiMarketsSource
        if isinstance(source, KalshiMarketsSource) and source.pending_results:
            results = source.pending_results
            result_hashes: set[str] = {r.get("raw_payload_hash", "") for r in results}
            try:
                storage.write("market_results", source.name, results, now)
                # Only persist seen tickers after the write succeeds — avoids
                # the race where tickers are marked seen before rows reach parquet.
                source.confirm_results_written()
                health.record_success(
                    source.name + "_results", now, len(results), result_hashes
                )
                if csv_rows is not None:
                    csv_rows.setdefault("market_results", []).extend(results)
            except Exception as exc:
                log.error("storage.write failed for market_results: %s", exc)

    label = now.strftime("%H:%MZ")
    with tqdm(
        total=len(eligible),
        desc=label,
        unit="src",
        bar_format="{desc} |{bar}| {n}/{total} [{elapsed}<{remaining}] {postfix}",
        file=sys.stderr,
        dynamic_ncols=True,
    ) as pbar:
        async def run_tracked(source: SourceClient):
            await run_source(source)
            pbar.set_postfix_str(source.name, refresh=False)
            pbar.update(1)

        await asyncio.gather(*[run_tracked(s) for s in eligible])

    # Write health log after every tick.
    try:
        health.write(now)
    except Exception as exc:
        log.error("health.write failed: %s", exc)


class Scheduler:
    """
    Manages the 10-minute poll loop with graceful shutdown.
    """

    def __init__(self, sources: list[SourceClient], stations: list) -> None:
        self._sources = sources
        self._stations = stations
        self._shutdown_event = asyncio.Event()
        self._semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        # Inject semaphore into all sources.
        for source in sources:
            source._semaphore = self._semaphore

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def handle_signal(signum):
            log.info("Received signal %s, shutting down...", signum)
            self._shutdown_event.set()
            # Cancel all running tasks so we don't block on thread executor downloads.
            for task in asyncio.all_tasks(loop):
                task.cancel()

        loop.add_signal_handler(signal.SIGINT, handle_signal, signal.SIGINT)
        loop.add_signal_handler(signal.SIGTERM, handle_signal, signal.SIGTERM)

    async def run_once(self) -> dict[str, list[dict]]:
        """
        Run exactly one poll cycle immediately.

        Returns a dict of table_name -> rows for --csv mode.
        """
        now = dt.datetime.now(dt.timezone.utc)
        csv_rows: dict[str, list[dict]] = {}
        await tick(now, self._sources, self._stations, csv_rows=csv_rows)
        return csv_rows

    async def run(self) -> None:
        """
        Run the poll loop until shutdown.

        Aligns polls to 10-minute wall-clock boundaries.
        """
        self._install_signal_handlers()
        log.info("Scheduler starting. Poll interval: %ds", POLL_INTERVAL_SEC)

        try:
            while not self._shutdown_event.is_set():
                now = dt.datetime.now(dt.timezone.utc)
                await tick(now, self._sources, self._stations)

                next_poll = _next_poll_time(dt.datetime.now(dt.timezone.utc))
                sleep_secs = (next_poll - dt.datetime.now(dt.timezone.utc)).total_seconds()
                if sleep_secs > 0:
                    log.info("Next poll at %s (sleep %.1fs)",
                             next_poll.isoformat(), sleep_secs)
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=sleep_secs,
                        )
                        break  # shutdown was requested
                    except asyncio.TimeoutError:
                        pass  # Normal: sleep elapsed, continue polling.

        finally:
            log.info("Scheduler shutting down. Flushing storage...")
            try:
                storage.flush_all()
            except Exception as exc:
                log.error("flush_all failed: %s", exc)

            final_ts = dt.datetime.now(dt.timezone.utc)
            try:
                health.write(final_ts)
            except Exception as exc:
                log.error("Final health write failed: %s", exc)

            log.info("Scheduler shutdown complete.")
