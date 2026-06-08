"""
CLI entrypoint for the weather features pipeline.

Usage:
    python -m weather_features.runner [--once] [--csv PATH] [--data-dir PATH] [--sources S1,S2]

Arguments:
    --once          Run one poll cycle immediately and exit.
    --csv PATH      After polling, write collected rows to CSV files at PATH/
                    (one file per table, named {table}.csv). Implies --once.
    --data-dir PATH Override the data root (default: repo root / "data").
    --sources S1,S2 Only run specified sources (comma-separated).
                    Default: all available sources.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Load .env from repo root before any imports that read env vars.
def _load_dotenv() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env_path = repo_root / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except ImportError:
            # Manual fallback if python-dotenv not installed.
            _manual_dotenv(env_path)


def _manual_dotenv(path: Path) -> None:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()


def _configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Quieten noisy libraries.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("herbie").setLevel(logging.WARNING)
    logging.getLogger("cfgrib").setLevel(logging.WARNING)


def _build_sources(source_filter: list[str] | None, data_dir: Path | None = None):
    """Instantiate all configured source clients."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from .sources.nws import NWSForecastSource
    from .sources.nbm import NBMSource
    from .sources.kalshi_markets import KalshiMarketsSource
    from .sources.gfs_mos import GFSMOSSource
    from .sources.gfs_lamp import GFSLAMPSource
    from .sources.metar import METARSource
    from .sources.cli import CLISource
    from .sources.climatology import CLIMOSource

    all_sources = [
        NWSForecastSource(),
        NBMSource(),
        KalshiMarketsSource(data_dir=data_dir),
        GFSMOSSource(),
        GFSLAMPSource(),
        METARSource(),
        CLISource(),
        CLIMOSource(data_dir=data_dir),
    ]

    # Herbie-backed sources: import conditionally so the pipeline runs
    # without Herbie installed (IEM-only mode).
    _herbie_sources = [
        ("ECMWF_DET", "ecmwf_det", "ECMWFDetSource"),
        ("ECMWF_ENS", "ecmwf_ens", "ECMWFEnsSource"),
        ("HRRR",      "hrrr",      "HRRRSource"),
        ("GEFS",      "gefs",      "GEFSSource"),
    ]
    try:
        import herbie  # noqa: F401
        herbie_available = True
    except ImportError:
        herbie_available = False
        _log.warning(
            "herbie-data not installed — ECMWF_DET, ECMWF_ENS, HRRR, GEFS sources disabled. "
            "Install herbie-data, cfgrib, and xarray to enable them."
        )

    if herbie_available:
        for source_name, module_name, class_name in _herbie_sources:
            try:
                mod = __import__(
                    f"weather_features.sources.{module_name}",
                    fromlist=[class_name],
                )
                cls = getattr(mod, class_name)
                all_sources.append(cls())
            except Exception as exc:
                _log.warning("Failed to load %s: %s", source_name, exc)

    _available_names = [s.name for s in all_sources]

    if source_filter:
        filter_set = {s.upper() for s in source_filter}
        all_sources = [s for s in all_sources if s.name.upper() in filter_set]
        if not all_sources:
            raise ValueError(
                f"No sources matched filter {source_filter}. "
                f"Available: {', '.join(_available_names)}"
            )

    return all_sources


async def _run_once(
    sources,
    stations: list,
    csv_dir: Path | None,
) -> None:
    from .scheduler import Scheduler
    from . import storage

    scheduler = Scheduler(sources, stations)
    csv_rows = await scheduler.run_once()
    storage.flush_all()

    if csv_dir and csv_rows:
        csv_dir.mkdir(parents=True, exist_ok=True)
        for table, rows in csv_rows.items():
            if rows:
                out_path = csv_dir / f"{table}.csv"
                storage.write_csv(rows, out_path)
                logging.getLogger(__name__).info(
                    "Wrote %d rows to %s", len(rows), out_path
                )


async def _run_loop(sources, stations: list) -> None:
    from .scheduler import Scheduler

    scheduler = Scheduler(sources, stations)
    await scheduler.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weather features data collection pipeline"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit.",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="Write collected rows to CSV files under PATH/ (implies --once).",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=None,
        help="Override data root directory (default: repo root / 'data').",
    )
    parser.add_argument(
        "--sources",
        metavar="SOURCE1,SOURCE2",
        default=None,
        help="Comma-separated list of sources to run (default: all).",
    )
    args = parser.parse_args()

    # Resolve data directory first so we can point the log file at it.
    if args.data_dir:
        data_root = Path(args.data_dir).resolve()
    else:
        # Default: repo root / "data". In Docker this is /app/data.
        data_root = Path(__file__).resolve().parents[2] / "data"

    _configure_logging(data_root / "weather.log")
    log = logging.getLogger(__name__)

    # Initialize storage and health tracking.
    from . import storage, health
    storage.init_storage(data_root)
    health.init_health(data_root)

    log.info("Data root: %s", data_root)

    # Load stations.
    from .stations import STATIONS
    stations = list(STATIONS.values())
    log.info("Loaded %d stations", len(stations))

    # Build sources.
    source_filter = (
        [s.strip() for s in args.sources.split(",") if s.strip()]
        if args.sources else None
    )
    try:
        sources = _build_sources(source_filter, data_dir=data_root)
    except ValueError as exc:
        log.error("Failed to build sources: %s", exc)
        sys.exit(1)

    log.info("Active sources: %s", [s.name for s in sources])

    # Determine run mode.
    run_once = args.once or bool(args.csv)
    csv_dir = Path(args.csv).resolve() if args.csv else None

    if run_once:
        log.info("Running single poll cycle (--once mode).")
        asyncio.run(_run_once(sources, stations, csv_dir))
        log.info("Done.")
    else:
        log.info("Starting continuous poll loop.")
        try:
            asyncio.run(_run_loop(sources, stations))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
