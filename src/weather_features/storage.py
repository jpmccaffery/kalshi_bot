"""
PyArrow-based storage layer for the weather features pipeline.

Key behavior:
- Buffers rows in memory per (table, source, poll_date).
- Flushes every 6 polls OR on shutdown OR when buffer exceeds 10k rows.
- Writes Parquet files with Zstd level-9 compression.
- market_snapshots and market_results go to data/market/ (not data/raw/).
- Static climatology goes to data/static/climatology/.
- Strict schema validation: unknown fields are dropped with a warning;
  missing required fields raise ValueError.

Data layout:
  data/raw/{table}/source={source}/poll_date={date}/part-{ts}-{uuid}.parquet
  data/market/{table}/source={source}/poll_date={date}/part-{ts}-{uuid}.parquet
  data/static/climatology/part-{ts}-{uuid}.parquet
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

# ── Schema definitions ──────────────────────────────────────────────────────

DAILY_FORECASTS_SCHEMA = pa.schema([
    ("poll_time",        pa.timestamp("us", tz="UTC")),
    ("source",           pa.string()),
    ("station_icao",     pa.string()),
    ("city",             pa.string()),
    ("target_date",      pa.date32()),
    ("kind",             pa.string()),
    ("value_f",          pa.float32()),
    ("p10",              pa.float32()),
    ("p25",              pa.float32()),
    ("p50",              pa.float32()),
    ("p75",              pa.float32()),
    ("p90",              pa.float32()),
    ("mean",             pa.float32()),
    ("sd",               pa.float32()),
    ("issued_at",        pa.timestamp("us", tz="UTC")),
    ("cycle",            pa.timestamp("us", tz="UTC")),
    ("fhr",              pa.int32()),
    ("raw_payload_hash", pa.string()),
    ("schema_version",   pa.int16()),
])

HOURLY_FORECASTS_SCHEMA = pa.schema([
    ("poll_time",        pa.timestamp("us", tz="UTC")),
    ("source",           pa.string()),
    ("station_icao",     pa.string()),
    ("city",             pa.string()),
    ("valid_time",       pa.timestamp("us", tz="UTC")),
    ("member",           pa.int16()),
    ("temp_f",           pa.float32()),
    ("dewpoint_f",       pa.float32()),
    ("wind_mph",         pa.float32()),
    ("wind_dir_deg",     pa.float32()),
    ("pressure_mb",      pa.float32()),
    ("sky_cover_pct",    pa.float32()),
    ("precip_in",        pa.float32()),
    ("issued_at",        pa.timestamp("us", tz="UTC")),
    ("cycle",            pa.timestamp("us", tz="UTC")),
    ("fhr",              pa.int32()),
    ("raw_payload_hash", pa.string()),
    ("schema_version",   pa.int16()),
])

OBSERVATIONS_SCHEMA = pa.schema([
    ("poll_time",        pa.timestamp("us", tz="UTC")),
    ("source",           pa.string()),
    ("station_icao",     pa.string()),
    ("city",             pa.string()),
    # METAR fields (null for CLI rows)
    ("observation_time", pa.timestamp("us", tz="UTC")),
    ("temp_f",           pa.float32()),
    ("dewpoint_f",       pa.float32()),
    ("wind_mph",         pa.float32()),
    ("wind_dir_deg",     pa.float32()),
    ("pressure_mb",      pa.float32()),
    ("sky_cover_pct",    pa.float32()),
    ("precip_in_1h",     pa.float32()),
    ("raw_metar",        pa.string()),
    ("is_speci",         pa.bool_()),
    # CLI fields (null for METAR rows)
    ("observation_date", pa.date32()),
    ("high_f",           pa.float32()),
    ("low_f",            pa.float32()),
    ("avg_f",            pa.float32()),
    ("precip_in",        pa.float32()),
    ("snow_in",          pa.float32()),
    ("issuance_time",    pa.timestamp("us", tz="UTC")),
    ("is_preliminary",   pa.bool_()),
    ("raw_text",         pa.string()),
    # Common
    ("raw_payload_hash", pa.string()),
    ("schema_version",   pa.int16()),
])

MARKET_SNAPSHOTS_SCHEMA = pa.schema([
    ("poll_time",        pa.timestamp("us", tz="UTC")),
    ("source",           pa.string()),
    ("ticker",           pa.string()),
    ("series",           pa.string()),
    ("expiry_date",      pa.date32()),
    ("yes_bid",          pa.float32()),
    ("yes_ask",          pa.float32()),
    ("no_bid",           pa.float32()),
    ("no_ask",           pa.float32()),
    ("last_price",       pa.float32()),
    ("volume",           pa.int32()),
    ("open_interest",    pa.int32()),
    ("status",           pa.string()),
    ("raw_payload_hash", pa.string()),
    ("schema_version",   pa.int16()),
])

MARKET_RESULTS_SCHEMA = pa.schema([
    ("poll_time",        pa.timestamp("us", tz="UTC")),
    ("source",           pa.string()),
    ("ticker",           pa.string()),
    ("series",           pa.string()),
    ("expiry_date",      pa.date32()),
    ("result",           pa.string()),
    ("close_time",       pa.timestamp("us", tz="UTC")),
    ("raw_payload_hash", pa.string()),
    ("schema_version",   pa.int16()),
])

CLIMATOLOGY_SCHEMA = pa.schema([
    ("station_icao",    pa.string()),
    ("month",           pa.int8()),
    ("day",             pa.int8()),
    ("normal_high_f",   pa.float32()),
    ("normal_low_f",    pa.float32()),
    ("stddev_high_f",   pa.float32()),
    ("stddev_low_f",    pa.float32()),
    ("source_dataset",  pa.string()),
    ("schema_version",  pa.int16()),
])

# Map table name → (schema, base_dir_segment)
# base_dir_segment is relative to the data root.
_TABLE_CONFIG: dict[str, tuple[pa.Schema, str]] = {
    "daily_forecasts":   (DAILY_FORECASTS_SCHEMA,   "raw/daily_forecasts"),
    "hourly_forecasts":  (HOURLY_FORECASTS_SCHEMA,  "raw/hourly_forecasts"),
    "observations":      (OBSERVATIONS_SCHEMA,      "raw/observations"),
    "market_snapshots":  (MARKET_SNAPSHOTS_SCHEMA,  "market/market_snapshots"),
    "market_results":    (MARKET_RESULTS_SCHEMA,    "market/market_results"),
    "climatology":       (CLIMATOLOGY_SCHEMA,       "static/climatology"),
}

FLUSH_EVERY_N_POLLS = 6
MAX_BUFFER_ROWS = 10_000

# ── Type alias ──────────────────────────────────────────────────────────────

_BufferKey = tuple[str, str, str]  # (table, source, poll_date_str)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _schema_for(table: str) -> pa.Schema:
    if table not in _TABLE_CONFIG:
        raise ValueError(f"Unknown table: {table!r}")
    return _TABLE_CONFIG[table][0]


def _partition_dir(data_root: Path, table: str, source: str, poll_date: dt.date) -> Path:
    _, base = _TABLE_CONFIG[table]
    if table == "climatology":
        # No source= or poll_date= partition for static data.
        return data_root / base
    return data_root / base / f"source={source}" / f"poll_date={poll_date}"


def _part_filename() -> str:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = uuid.uuid4().hex[:8]
    return f"part-{ts}-{uid}.parquet"


def _coerce_row(row: dict, schema: pa.Schema) -> dict:
    """
    Coerce a row dict to match the schema:
    - Drop unknown fields (with a warning).
    - Raise ValueError for missing required (non-nullable) fields.
    - Pass-through None for nullable fields.
    """
    schema_names = {field.name for field in schema}
    unknown = set(row.keys()) - schema_names
    if unknown:
        log.warning("Dropping unknown fields from row: %s", unknown)

    coerced: dict[str, Any] = {}
    for field in schema:
        name = field.name
        if name not in row:
            # All fields are "optional" in the sense that we allow null values,
            # but certain sentinel fields (poll_time, source) must be present.
            if name in ("poll_time", "source", "raw_payload_hash", "schema_version"):
                raise ValueError(f"Required field {name!r} missing from row")
            coerced[name] = None
        else:
            coerced[name] = row[name]

    return coerced


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    """Convert a list of coerced row dicts to a PyArrow Table."""
    if not rows:
        return pa.table({field.name: [] for field in schema}, schema=schema)

    columns: dict[str, list] = {field.name: [] for field in schema}
    for row in rows:
        for field in schema:
            columns[field.name].append(row.get(field.name))

    arrays = []
    for field in schema:
        col_data = columns[field.name]
        try:
            arr = pa.array(col_data, type=field.type)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
            raise ValueError(
                f"Column {field.name!r} failed type coercion to {field.type}: {exc}"
            ) from exc
        arrays.append(arr)

    return pa.table(arrays, schema=schema)


# ── StorageManager ──────────────────────────────────────────────────────────

class StorageManager:
    """
    Manages buffered Parquet writes for the weather pipeline.

    Thread-safety: designed for single-threaded asyncio use. Do not call
    from multiple threads.
    """

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        # Buffer: key -> list of coerced row dicts
        self._buffer: dict[_BufferKey, list[dict]] = defaultdict(list)
        # Poll count per key for flush triggering.
        self._poll_count: dict[_BufferKey, int] = defaultdict(int)

    def write(self, table: str, source: str, rows: list[dict],
              poll_time: dt.datetime) -> None:
        """
        Validate and buffer rows. Flush if thresholds are met.

        Args:
            table: target table name
            source: source name (used for partitioning)
            rows: list of row dicts; each must conform to the table schema
            poll_time: UTC datetime of this poll cycle
        """
        if not rows:
            return

        schema = _schema_for(table)
        poll_date = poll_time.date() if poll_time.tzinfo else poll_time.date()
        key: _BufferKey = (table, source, str(poll_date))

        coerced_rows: list[dict] = []
        for row in rows:
            try:
                coerced_rows.append(_coerce_row(row, schema))
            except ValueError as exc:
                log.error("Schema validation error in table=%s source=%s: %s",
                          table, source, exc)
                raise

        self._buffer[key].extend(coerced_rows)
        self._poll_count[key] += 1

        total_buffered = len(self._buffer[key])
        if (self._poll_count[key] >= FLUSH_EVERY_N_POLLS or
                total_buffered >= MAX_BUFFER_ROWS):
            self._flush_key(key, poll_date_str=str(poll_date))

    def flush_all(self) -> None:
        """Flush all buffered data to disk. Call on shutdown."""
        for key in list(self._buffer.keys()):
            if self._buffer[key]:
                _, _, poll_date_str = key
                self._flush_key(key, poll_date_str=poll_date_str)

    def _flush_key(self, key: _BufferKey, poll_date_str: str) -> None:
        rows = self._buffer.get(key, [])
        if not rows:
            return

        table_name, source, _ = key
        poll_date = dt.date.fromisoformat(poll_date_str)
        schema = _schema_for(table_name)
        out_dir = _partition_dir(self._data_root, table_name, source, poll_date)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / _part_filename()

        try:
            arrow_table = _rows_to_table(rows, schema)
            pq.write_table(
                arrow_table,
                str(out_path),
                compression="zstd",
                compression_level=9,
            )
            log.info("Flushed %d rows to %s", len(rows), out_path)
        except Exception as exc:
            log.error("Failed to write parquet %s: %s", out_path, exc)
            raise
        finally:
            self._buffer[key] = []
            self._poll_count[key] = 0

    def write_csv(self, rows: list[dict], out_path: Path) -> None:
        """Write a list of row dicts to a CSV file."""
        if not rows:
            log.warning("write_csv called with empty rows, skipping %s", out_path)
            return
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(rows[0].keys())
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        log.info("Wrote %d rows to CSV %s", len(rows), out_path)


# ── Module-level singleton ───────────────────────────────────────────────────

_manager: Optional[StorageManager] = None


def _get_manager() -> StorageManager:
    if _manager is None:
        raise RuntimeError("Storage not initialized. Call init_storage() first.")
    return _manager


def init_storage(data_root: Path) -> None:
    """Initialize the module-level storage manager."""
    global _manager
    _manager = StorageManager(data_root)
    log.info("Storage initialized at %s", data_root)


def write(table: str, source: str, rows: list[dict],
          poll_time: dt.datetime) -> None:
    """Write rows to the given table. See StorageManager.write()."""
    _get_manager().write(table, source, rows, poll_time)


def flush_all() -> None:
    """Flush all buffered data. See StorageManager.flush_all()."""
    _get_manager().flush_all()


def write_csv(rows: list[dict], out_path: Path) -> None:
    """Write rows to a CSV file."""
    _get_manager().write_csv(rows, out_path)
