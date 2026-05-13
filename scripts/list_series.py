#!/usr/bin/env python3
"""
List all Kalshi series.

A series is the top-level grouping (e.g. "KXINX" — S&P 500 daily close).
Each series contains one or more events, which contain the individual markets.

Usage
-----
    python scripts/list_series.py
    python scripts/list_series.py --output series.csv
    python scripts/list_series.py --live --output series_live.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _kalshi_api import (
    base_arg_parser, demo_override_from_args,
    get_all_pages, load_credentials, write_output,
)


def main() -> None:
    parser = base_arg_parser("Fetch all Kalshi series.")
    args   = parser.parse_args()

    key_id, private_key, base_url = load_credentials(
        demo_override=demo_override_from_args(args)
    )
    series = get_all_pages(
        base_url       = base_url,
        key_id         = key_id,
        private_key    = private_key,
        path           = "/trade-api/v2/series",
        collection_key = "series",
    )

    print(f"Fetched {len(series)} series.", file=sys.stderr)
    write_output(series, args, priority_cols=[
        "ticker", "title", "category", "tags",
        "frequency", "status",
    ])


if __name__ == "__main__":
    main()
