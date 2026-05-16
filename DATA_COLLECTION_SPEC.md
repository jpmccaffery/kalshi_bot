# Data Collection Spec: Multi-Source Weather Feature Pipeline (v2)

## Goal

Collect 1-3 months of weather forecast and observation data, polled every
10 minutes, for the 39 cities the bot trades on Kalshi. The output is a
Parquet dataset of raw source payloads suitable for downstream feature
engineering and predictive modeling.

This is a research data pipeline, not a production trading component.
Latency does not matter. Completeness, provenance, and faithfulness to
the source's native shape matter.

## Design philosophy

**Store data in the shape the source emits it. Do not aggregate, derive,
or transform at ingestion time.**

- Sources that emit hourly trajectories → stored as hourly rows.
- Sources that emit daily summaries → stored as daily rows.
- Sources that emit observations → stored as observation rows.
- All transformations (hourly→daily aggregation, computing percentiles
  from ensemble members, joining sources together, late-day Bayesian
  updates from METAR + forecast) happen in a separate `derivations.py`
  module at query time, not at ingestion.

**Every poll writes a row.** Even if the underlying data hasn't changed
from the previous poll, we write a new row with the same content. This:
- Makes the table a faithful event log keyed on poll_time.
- Makes outage detection trivial (a missing row means the poll failed).
- Allows point-in-time queries with a simple equality on poll_time
  instead of a window query.
- Costs disk but disk is cheap and Parquet compresses duplicates well.

**Payload hash on every row.** Every row carries a SHA256 of the
source's raw response. This lets us answer "did this poll's content
differ from the previous?" without needing a separate change-detection
mechanism, and provides an audit trail.

## Scope

In scope:
- Forecast data from 8 source families
- Observation data (METAR + final CLI ground truth)
- Climatology baseline (one-time pull)
- Parquet output, partitioned by source and poll-date

Out of scope (Claude Code should NOT build these as part of this work):
- Kalshi market data ingestion (already handled by existing code)
- Any aggregation, derivation, or transformation logic beyond what's
  needed to parse the raw source response into rows
- Joining sources together
- Feature engineering
- Model training, backtesting, calibration
- Any code in the existing `forecast/` package (the production recommender)

A stub for `derivations.py` is included so the shape is documented, but
its functions should be left as `pass` for this PR. Filling them in is
later work.

## Architecture

```
weather_features/
├── sources/
│   ├── base.py              # SourceClient ABC; uniform poll + caching
│   ├── nws.py               # api.weather.gov deterministic
│   ├── nbm.py               # NBM NBP percentile text bulletins
│   ├── gfs_mos.py           # GFS MOS bulletins (station-keyed, daily)
│   ├── gfs_lamp.py          # GFS LAMP bulletins (hourly, 1-25h)
│   ├── ecmwf_ifs.py         # ECMWF IFS deterministic (hourly trajectory)
│   ├── ecmwf_ens.py         # ECMWF IFS 51-member ensemble
│   ├── hrrr.py              # HRRR 3km CONUS (hourly trajectory)
│   ├── gefs.py              # GEFS 31-member ensemble
│   ├── metar.py             # Hourly observations from settlement station
│   ├── cli.py               # Daily Climate Report (ground truth)
│   └── climatology.py       # One-time pull of 1991-2020 normals
├── stations.py              # Reuse from forecast/stations.py
├── storage.py               # Parquet writer with per-table schema enforcement
├── scheduler.py             # 10-min uniform poll loop
├── runner.py                # Main entrypoint, systemd-friendly
├── derivations.py           # STUB MODULE — function signatures only
└── tests/
```

Reuse `forecast/stations.py` from the existing forecast package — do not
duplicate it.

## The four tables

All forecasts and observations land in one of three "raw" tables:

```
data/raw/
├── hourly_forecasts/        # Hourly trajectories (ECMWF, HRRR, GEFS, LAMP)
│   └── source=<NAME>/poll_date=<YYYY-MM-DD>/part-*.parquet
├── daily_forecasts/         # Native daily summaries (NBM, MOS, NWS daily)
│   └── source=<NAME>/poll_date=<YYYY-MM-DD>/part-*.parquet
└── observations/            # METAR hourly + CLI daily ground truth
    └── source=<NAME>/poll_date=<YYYY-MM-DD>/part-*.parquet
```

Plus one separate small table:

```
data/static/
└── climatology/             # 1991-2020 normals, one row per (station, mm-dd)
    └── part-*.parquet
```

`poll_date=` partition is the **UTC date the poll occurred**, not the
target forecast date. This makes "load yesterday's polls" a partition
prune.

## Source taxonomy

| Source | Family | Cycle cadence | Native shape | Table |
|---|---|---|---|---|
| NWS api.weather.gov | NWS_FORECAST | hourly-ish | daily summaries | daily_forecasts |
| NBM NBP bulletins | NBM | every 6h | daily percentile distributions | daily_forecasts |
| GFS MOS | GFS_MOS | 4x daily | daily summaries | daily_forecasts |
| GFS LAMP | GFS_LAMP | hourly | hourly trajectory | hourly_forecasts |
| ECMWF IFS deterministic | ECMWF_DET | 4x daily | hourly trajectory | hourly_forecasts |
| ECMWF IFS ensemble | ECMWF_ENS | 4x daily | hourly × 51 members | hourly_forecasts |
| HRRR | HRRR | hourly | hourly trajectory | hourly_forecasts |
| GEFS | GEFS | 4x daily | hourly × 31 members | hourly_forecasts |
| METAR observations | METAR | sub-hourly | hourly observations | observations |
| CLI daily climate report | CLI | daily | one row per day | observations |
| 1991-2020 normals | CLIMO | static | one row per station-date | climatology |

## Schemas

### daily_forecasts

For sources that natively emit "the high/low for date D is X" without
us having to derive it.

```python
{
    "poll_time": "timestamp[us, UTC]",
    "source": "string",
    "station_icao": "string",
    "city": "string",
    "target_date": "date32",                  # LST calendar date
    "kind": "string",                         # "high" or "low"
    "value_f": "float32",                     # Point forecast (or p50)
    "p10": "float32",                         # null if not probabilistic
    "p25": "float32",
    "p50": "float32",
    "p75": "float32",
    "p90": "float32",
    "mean": "float32",
    "sd": "float32",
    "issued_at": "timestamp[us, UTC]",
    "cycle": "timestamp[us, UTC]",            # null for non-model sources
    "fhr": "int32",                           # Forecast hour from cycle
    "raw_payload_hash": "string",             # SHA256 hex
    "schema_version": "int16",                # Starts at 1
}
```

### hourly_forecasts

For sources that emit hourly time series. One row per (poll, source,
station, valid_time, member). Ensemble members get one row per member
per valid_time.

```python
{
    "poll_time": "timestamp[us, UTC]",
    "source": "string",
    "station_icao": "string",
    "city": "string",
    "valid_time": "timestamp[us, UTC]",
    "member": "int16",                        # 0 for deterministic
    "temp_f": "float32",                      # 2m temperature
    "dewpoint_f": "float32",
    "wind_mph": "float32",
    "wind_dir_deg": "float32",
    "pressure_mb": "float32",
    "sky_cover_pct": "float32",
    "precip_in": "float32",
    "issued_at": "timestamp[us, UTC]",
    "cycle": "timestamp[us, UTC]",
    "fhr": "int32",
    "raw_payload_hash": "string",
    "schema_version": "int16",
}
```

The hash is the same value for every row produced from a single source
response. Compute once per fetch, copy to every row.

### observations

Two sub-types live in this table, distinguished by `source`.

**METAR rows:**
```python
{
    "poll_time": "timestamp[us, UTC]",
    "source": "string",                       # "METAR"
    "station_icao": "string",
    "city": "string",
    "observation_time": "timestamp[us, UTC]",
    "temp_f": "float32",
    "dewpoint_f": "float32",
    "wind_mph": "float32",
    "wind_dir_deg": "float32",
    "pressure_mb": "float32",
    "sky_cover_pct": "float32",
    "precip_in_1h": "float32",
    "raw_metar": "string",
    "is_speci": "bool",                       # Special off-schedule report
    "raw_payload_hash": "string",
    "schema_version": "int16",
}
```

**CLI rows:**
```python
{
    "poll_time": "timestamp[us, UTC]",
    "source": "string",                       # "CLI"
    "station_icao": "string",
    "city": "string",
    "observation_date": "date32",
    "high_f": "float32",
    "low_f": "float32",
    "avg_f": "float32",
    "precip_in": "float32",
    "snow_in": "float32",
    "issuance_time": "timestamp[us, UTC]",
    "is_preliminary": "bool",
    "raw_text": "string",
    "raw_payload_hash": "string",
    "schema_version": "int16",
}
```

Keep these in separate Parquet files within the partition — the
`source=` directory does this naturally.

### climatology (static)

```python
{
    "station_icao": "string",
    "month": "int8",
    "day": "int8",
    "normal_high_f": "float32",
    "normal_low_f": "float32",
    "record_high_f": "float32",
    "record_low_f": "float32",
    "source_dataset": "string",
    "schema_version": "int16",
}
```

## Source-by-source notes

### NWS_FORECAST → daily_forecasts

**Source:** api.weather.gov, two-step `/points` → `/gridpoints/{wfo}/{x},{y}`.
The existing `forecast/nws_client.py` does this. Reuse it.

**Native shape:** `maxTemperature` and `minTemperature` arrays already
binned per local calendar day, each with a `validTime` like
`"2026-05-15T18:00:00+00:00/PT12H"`.

**Row production:** one row per (station, target_date, kind) per poll.
`value_f` from the max/min value; percentile columns null; `issued_at`
from the response's `updateTime`; `cycle` and `fhr` null (NWS forecasts
aren't model-cycle-keyed).

### NBM → daily_forecasts

**Source:** NOMADS NBP text bulletins at
`https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/blend.YYYYMMDD/CC/text/blend_nbptx.tCCz`

The existing `forecast/nbm_client.py` parses these. Reuse it.

**Native shape:** for each (station, target_date, kind), the bulletin
gives `mean`, `sd`, `p10`, `p25`, `p50`, `p75`, `p90`.

**Row production:** one row per (station, target_date, kind) per poll.
Fill all percentile columns. `value_f` = `p50`. `cycle` from the
bulletin header, `fhr` from the column header.

**Cache behavior:** NBP cycles are 6 hours apart and become available
~90 min after the cycle hour. The fetch logic should compute the latest
published cycle and only re-download when a new cycle is available. But
still write a row every poll with the cached content. The hash will be
identical to the previous row, which is the signal that nothing changed.

### GFS_MOS → daily_forecasts

**Source:** Iowa State IEM API:
`https://mesonet.agron.iastate.edu/api/1/mos.json?station=<ICAO>&model=GFS`

**Native shape:** the MOS bulletin includes daily N (min) and X (max)
values keyed by forecast hour. Pull the N/X pairs and convert each into
a daily_forecasts row.

**Row production:** one row per (station, target_date, kind) per poll.
`value_f` from the N or X value. Percentile columns null. `cycle` from
the response's `runtime` field; `fhr` from the column the value came
from.

### GFS_LAMP → hourly_forecasts

**Source:** Same IEM API with `model=LAV`.

**Native shape:** hourly station forecasts at lead times 1-25h, with
temperature, dewpoint, wind, sky cover, precip probability per hour.

**Row production:** one row per (station, valid_time) per poll. Member
is 0 (LAMP is deterministic). `cycle` from `runtime`, `fhr` from each
lead hour.

### ECMWF_DET → hourly_forecasts

**Source:** ECMWF open data via Herbie:
```python
H = Herbie(cycle_dt, model="ifs", product="oper", fxx=fhr)
H.xarray("2t")
```

Open data is on AWS S3 mirror, free, no auth. ECMWF transitioned to
fully-open IFS on October 1 2025.

**Cycles:** 00/06/12/18Z, available ~6-8h after cycle for the open feed.
Forecast hours 0-90 at 1h resolution, then 3h, then 6h. We need 0-96h
covered.

**Native shape:** gridded 0.25° global field at each forecast hour.

**Row production:** bilinear-interpolate to each station's lat/lon at
each forecast hour. One row per (station, valid_time) per poll. Member
= 0 (deterministic). Other surface vars (dewpoint, wind, etc.) are
available from the same GRIB file — pull them too.

**Don't pull the full global grid every poll.** Use Herbie's spatial
subsetting or extract just the neighborhood around all 39 stations once
per cycle, then interpolate from the subset.

### ECMWF_ENS → hourly_forecasts

**Source:** Herbie:
```python
H = Herbie(cycle_dt, model="ifs", product="enfo", fxx=fhr)
```

The ENFO stream is the 51-member ensemble at 0.25° resolution.

**Native shape:** for each forecast hour, 51 fields (one per member).

**Row production:** one row per (station, valid_time, member) per poll.
51 members × 39 stations × hourly forecast hours × 4 cycles/day is a
lot of rows. Compress with Zstd level 9.

### HRRR → hourly_forecasts

**Source:** Herbie:
```python
H = Herbie(cycle_dt, model="hrrr", product="sfc", fxx=fhr)
H.xarray("TMP:2 m")
```

**Cycles:** hourly. 00/06/12/18Z runs go to F48; other hours go to F18.

**Row production:** bilinear-interpolate to station lat/lon. Member = 0.

### GEFS → hourly_forecasts

**Source:** Herbie:
```python
H = Herbie(cycle_dt, model="gefs", product="atmos.5", fxx=fhr, member=N)
```

**Cycles:** 4x daily. 31 members. Forecast to 384h (we only need ~168h).

**Row production:** one row per (station, valid_time, member). Member
range 0-30.

### METAR → observations

**Source:** IEM ASOS API for historical pulls:
`https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=<ICAO>&data=all&year1=...`

NWS for the latest observation:
`https://api.weather.gov/stations/<ICAO>/observations/latest`

**Strategy:** every 10 min, pull the last 24 hours of METAR for each
station. Catches SPECI updates and retroactive corrections.

**Row production:** one row per (station, observation_time) per poll.
Yes, the same observation gets written ~144 times across the polls of
its 24-hour window. Disk is cheap; downstream dedup is a trivial
group-by on `raw_payload_hash`.

### CLI → observations

**Source:** IEM parsed CLI:
`https://mesonet.agron.iastate.edu/api/1/nwstext_search.json?sts=<start>&ets=<end>&awips=CLI<3char>`

The 3-character AWIPS suffix is station-specific. Map these explicitly
in `stations.py` (e.g. NYC = CLINYC, ORD = CLIORD).

**Strategy:** poll once per hour (no need every 10 min for daily
reports). Preliminary CLI typically arrives 6-10am local the morning
after the observation day. A final/corrected CLI may follow within 24
hours.

**Row production:** one row per (station, observation_date,
is_preliminary) per poll. A given day eventually has 2 rows: one
preliminary, one final. Both stored.

### CLIMO → climatology (one-time)

**Source:** NCEI 1991-2020 Daily Normals:
`https://www.ncei.noaa.gov/data/normals-daily/1991-2020/access/<ID>.csv`

**Strategy:** download once. Write to `data/static/climatology/`. Skip
on subsequent runs if the file exists.

## Scheduler

A single asyncio loop fires every 10 minutes:

```python
POLL_INTERVAL_SEC = 600

async def main_loop():
    while True:
        now = datetime.now(timezone.utc)
        await tick(now)
        await asyncio.sleep_until_next_poll(POLL_INTERVAL_SEC)

async def tick(now):
    tasks = []
    for source in SOURCES:
        if now - source.last_poll_at < source.min_poll_interval_sec:
            continue
        tasks.append(source.poll(now, STATIONS))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for source, result in zip(SOURCES, results):
        if isinstance(result, Exception):
            log.error("source=%s error=%s", source.name, result)
            health.record_failure(source.name, now, result)
            continue
        storage.write(source.table, source.name, result, poll_time=now)
        health.record_success(source.name, now, len(result.rows))
```

Each `source.poll(now, stations)` handles its own per-cycle caching.
The contract: it always returns a list of rows (possibly empty). It
does not raise unless something unrecoverable happened.

**Critical:** sources should NOT skip writes when content is unchanged.
Every successful poll produces rows; every row gets written. The hash
column is how duplicates are identified later.

**Per-source poll frequency:** by default every tick (10 min). CLI is
the exception — it polls every 6th tick (hourly). Each source declares
its own minimum poll interval:

```python
class SourceClient:
    name: str
    table: str   # "hourly_forecasts" | "daily_forecasts" | "observations"
    min_poll_interval_sec: int = 600   # default = every tick
```

**Rate limiting:** wrap concurrent HTTP requests in
`asyncio.Semaphore(8)` to avoid overwhelming NOMADS. ECMWF's 500-conn
limit is far above our usage.

**Clock alignment:** align polls to 10-min wall-clock boundaries (00,
10, 20, ...) rather than `last_poll + 600s`. Missed polls shouldn't
drift the schedule.

## Storage layer

`storage.py` exposes one function:

```python
def write(table: str, source: str, rows: list[dict], poll_time: datetime):
    """
    Append rows to the appropriate partition. Validates against the
    table's schema; raises if any row doesn't conform.
    """
```

Implementation:
- One PyArrow `Table` per `(table, source, poll_date)` partition.
- Append rows to an in-memory buffer; flush every 6 polls (~1 hour) per
  partition.
- On flush, write a new part file:
  `part-<utc_timestamp>-<uuid>.parquet`.
- Zstd compression level 9.
- On graceful shutdown (SIGTERM, SIGINT), flush all buffers before
  exiting.
- Schema enforcement is strict: a row missing a required field raises
  and the scheduler logs an error. Fail loudly rather than corrupt the
  table.

## Health monitoring

`health.py` writes a daily summary to `data/health/<YYYY-MM-DD>.json`:

```json
{
  "date": "2026-05-15",
  "sources": {
    "NBM": {
      "polls_attempted": 144,
      "polls_succeeded": 142,
      "polls_failed": 2,
      "rows_written": 22152,
      "unique_payload_hashes": 5,
      "errors": [
        {"time": "...", "error": "..."}
      ]
    }
  },
  "disk_usage_mb": 187
}
```

`unique_payload_hashes` is the key field for detecting silent failures.
If NBM polls every 10 min for 24h and produces only 1 unique hash, the
upstream is stuck.

## derivations.py — STUB FOR FUTURE WORK

Create the file with function signatures and docstrings; bodies are
`raise NotImplementedError`. The purpose is to document the shape of
later work so the raw schemas above support it.

```python
"""
Derived views over the raw data. NOT IMPLEMENTED in this PR — these
are stubs documenting the intended shape of future feature engineering.
"""
from __future__ import annotations
import datetime as dt
from typing import Optional


def daily_summary_from_hourly(
    poll_time: dt.datetime,
    source: str,
    station_icao: str,
    target_date: dt.date,
    kind: str,
    station_tz_offset: int,
) -> Optional[dict]:
    """
    Aggregate one source's hourly forecast trajectory into a daily
    high/low for the given LST calendar date.

    For deterministic sources: returns {"value_f": ...}.
    For ensemble sources: returns {"value_f": mean, "members": [...]}.
    Returns None if the hourly data doesn't cover the target window.

    The LST window for kind="high" on target_date=D is approximately
    [D 00:00 LST, D 24:00 LST]. For kind="low" it is
    [D-1 18:00 LST, D 12:00 LST] (the overnight period ending that
    morning), matching the NWS CLI reporting convention.

    Be careful: this function defines what "daily high/low" means
    everywhere downstream. Don't reimplement it in notebooks.
    """
    raise NotImplementedError


def unified_daily_view(
    poll_time: dt.datetime,
    station_icao: str,
    target_date: dt.date,
    kind: str,
) -> dict[str, dict]:
    """
    Return a dict keyed by source name, where each value is the daily
    summary for that source at poll_time. For native-daily sources,
    reads from daily_forecasts. For hourly sources, calls
    daily_summary_from_hourly.

    This is the table the feature pipeline consumes.
    """
    raise NotImplementedError


def latest_payload(
    poll_time: dt.datetime,
    source: str,
    station_icao: str,
    table: str,
) -> Optional[dict]:
    """
    Find the most recent row written for this source/station as of
    poll_time. Used when we want to ask 'what did source S know at
    time T?' rather than 'what did source S say at exactly poll T?'.
    """
    raise NotImplementedError


def nowcast_high(
    poll_time: dt.datetime,
    station_icao: str,
    target_date: dt.date,
    station_tz_offset: int,
) -> Optional[float]:
    """
    Combine METAR observations (already-realized temps for today) with
    HRRR's remaining hourly forecast to produce the best estimate of
    today's high given everything known at poll_time. Only meaningful
    when poll_time falls within target_date in LST.
    """
    raise NotImplementedError
```

## Implementation order

Build incrementally. Do NOT attempt to ship all 11 sources at once.

1. **Skeleton.** `base.py`, `storage.py`, `scheduler.py`, `runner.py`.
   Hardcode one fake source returning a single row. Verify Parquet
   roundtrip and partition structure work end-to-end.
2. **Stations + reuse.** Wire in `forecast/stations.py`. Verify the
   station list loads correctly.
3. **Reuse existing clients.** Add NWS_FORECAST and NBM sources by
   wrapping `forecast/nws_client.py` and `forecast/nbm_client.py`.
   Verify both write valid daily_forecasts rows.
4. **IEM-backed sources.** GFS_MOS, GFS_LAMP, METAR, CLI. All use the
   IEM API and return JSON.
5. **Climatology.** One-time pull, write to `data/static/`.
6. **Herbie installation.** Install Herbie + eccodes + cfgrib. Verify
   `H.xarray("TMP:2 m")` works for a single HRRR file.
7. **Herbie-backed sources, deterministic first.** ECMWF_DET, HRRR.
   Verify hourly_forecasts rows look right for one station, one cycle.
8. **Herbie-backed sources, ensembles.** ECMWF_ENS, GEFS. Disk usage
   will jump significantly — watch the health log.
9. **Run for 48 hours.** Verify all 11 sources are producing data,
   check disk usage, error rates, schema correctness.
10. **Add `derivations.py` stub.** Function signatures only, no bodies.
11. **Hand off.** The pipeline runs unattended for the next 4-12 weeks.

## Disk usage estimate (under every-poll-writes)

39 stations, 144 polls/day:

| Source | Rows/day | MB/day (Zstd) |
|---|---|---|
| NWS_FORECAST | ~22K | 4 |
| NBM | ~22K | 5 |
| GFS_MOS | ~22K | 4 |
| GFS_LAMP | ~140K | 25 |
| ECMWF_DET | ~540K | 50 |
| ECMWF_ENS | ~27M | 200 |
| HRRR | ~270K | 50 |
| GEFS | ~17M | 130 |
| METAR | ~135K | 20 |
| CLI | ~2K | 1 |
| **Total** | **~45M** | **~490 MB/day** |

Three months: ~45 GB. Reasonable for a workstation.

Zstd compresses well on duplicated content (every-poll writes will mostly
be identical for slow sources), so the real number will likely be
lower. Measure after week 1 and adjust the budget if needed.

## Dependencies

```
pyarrow >= 15
herbie-data >= 2024.8
cfgrib >= 0.9
xarray >= 2024.0
requests >= 2.31
aiohttp >= 3.9
ecmwf-opendata >= 0.3   # optional, if not relying solely on Herbie
```

`cfgrib` requires `eccodes`, most reliably installed via conda from
conda-forge. Document this in the project README.

## Operational requirements

- `USER_AGENT` set on every HTTP client to a string identifying the bot
  plus a contact email. NWS will silently 403 without it.
- Run under `systemd` (or equivalent process supervisor) with auto-
  restart on failure. **Do not** run in a tmux session or one-off Python
  process — uninterrupted operation for weeks is required.
- Log to a rotating file, not just stdout.
- On startup, scan existing partitions and continue cleanly. The
  pipeline must be safe to restart at any time.

## What this PR delivers

A running pipeline that, every 10 minutes, polls 11 source families
for 39 cities and writes raw rows to a Parquet dataset in faithful
native shape. A stub `derivations.py` documenting the intended shape
of downstream work. A daily health summary. Nothing else.

No aggregations. No joins. No model code. No feature engineering. No
modifications to the existing `forecast/` package.

Keep it scoped.
