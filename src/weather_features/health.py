"""
Health monitoring for the weather features pipeline.

Writes data/health/YYYY-MM-DD.json after each poll cycle.

Tracks per source:
- polls_attempted
- polls_succeeded
- polls_failed
- rows_written
- unique_payload_hashes (set of seen hashes → emitted as count)
- errors: list of {time, error} dicts

Also records overall disk usage of the data directory.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class _SourceStats:
    def __init__(self) -> None:
        self.polls_attempted: int = 0
        self.polls_succeeded: int = 0
        self.polls_failed: int = 0
        self.rows_written: int = 0
        self.unique_payload_hashes: set[str] = set()
        self.errors: list[dict] = []

    def to_dict(self) -> dict:
        return {
            "polls_attempted": self.polls_attempted,
            "polls_succeeded": self.polls_succeeded,
            "polls_failed": self.polls_failed,
            "rows_written": self.rows_written,
            "unique_payload_hashes": len(self.unique_payload_hashes),
            "errors": self.errors,
        }


class HealthTracker:
    """
    Tracks per-source health metrics and writes daily JSON summaries.
    """

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        self._health_dir = data_root / "health"
        # Key: (date_str, source_name)
        self._stats: dict[str, dict[str, _SourceStats]] = defaultdict(
            lambda: defaultdict(_SourceStats)
        )

    def _date_key(self, ts: dt.datetime) -> str:
        return ts.strftime("%Y-%m-%d")

    def record_attempt(self, source: str, ts: dt.datetime) -> None:
        self._stats[self._date_key(ts)][source].polls_attempted += 1

    def record_success(self, source: str, ts: dt.datetime, rows_written: int,
                       hashes: Optional[set[str]] = None) -> None:
        stats = self._stats[self._date_key(ts)][source]
        stats.polls_succeeded += 1
        stats.rows_written += rows_written
        if hashes:
            stats.unique_payload_hashes.update(hashes)

    def record_failure(self, source: str, ts: dt.datetime, error: Exception) -> None:
        stats = self._stats[self._date_key(ts)][source]
        stats.polls_failed += 1
        stats.errors.append({
            "time": ts.isoformat(),
            "error": str(error),
        })

    def write(self, ts: dt.datetime) -> None:
        """Write the health JSON for the current UTC date."""
        date_str = self._date_key(ts)
        self._health_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._health_dir / f"{date_str}.json"

        disk_mb = _disk_usage_mb(self._data_root)

        sources_data: dict[str, dict] = {}
        for source, stats in self._stats[date_str].items():
            sources_data[source] = stats.to_dict()

        payload = {
            "date": date_str,
            "sources": sources_data,
            "disk_usage_mb": disk_mb,
        }

        try:
            with open(out_path, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            log.error("Failed to write health log %s: %s", out_path, exc)


def _disk_usage_mb(path: Path) -> float:
    """Return total disk usage of a directory in MB."""
    try:
        total = shutil.disk_usage(path).used
        return round(total / (1024 * 1024), 1)
    except Exception:
        try:
            total_bytes = sum(
                f.stat().st_size
                for f in path.rglob("*")
                if f.is_file()
            )
            return round(total_bytes / (1024 * 1024), 1)
        except Exception:
            return 0.0


# ── Module-level singleton ───────────────────────────────────────────────────

_tracker: Optional[HealthTracker] = None


def init_health(data_root: Path) -> None:
    """Initialize the module-level health tracker."""
    global _tracker
    _tracker = HealthTracker(data_root)


def _get_tracker() -> HealthTracker:
    if _tracker is None:
        raise RuntimeError("Health not initialized. Call init_health() first.")
    return _tracker


def record_attempt(source: str, ts: dt.datetime) -> None:
    _get_tracker().record_attempt(source, ts)


def record_success(source: str, ts: dt.datetime, rows_written: int,
                   hashes: Optional[set[str]] = None) -> None:
    _get_tracker().record_success(source, ts, rows_written, hashes)


def record_failure(source: str, ts: dt.datetime, error: Exception) -> None:
    _get_tracker().record_failure(source, ts, error)


def write(ts: dt.datetime) -> None:
    _get_tracker().write(ts)
