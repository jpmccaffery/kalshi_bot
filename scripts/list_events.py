#!/usr/bin/env python3
"""
List all open Kalshi events (optionally filtered by series ticker).

An event is a single question instance (e.g. "Will the S&P 500 close above
4500 on Jan 15?").  Each event contains one or more YES/NO markets.

Usage
-----
    python scripts/list_events.py
    python scripts/list_events.py --series KXINX
    python scripts/list_events.py --output events.csv
    python scripts/list_events.py --live --series KXINX --output events_live.csv
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
    parser = base_arg_parser("Fetch all open Kalshi events.")
    parser.add_argument(
        "--series", "-s",
        metavar="TICKER",
        help="Filter to events belonging to this series ticker (e.g. KXINX).",
    )
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Include closed/settled events, not just open ones.",
    )
    args = parser.parse_args()

    key_id, private_key, base_url = load_credentials(
        demo_override=demo_override_from_args(args)
    )

    params: dict = {}
    if not args.all_statuses:
        params["status"] = "open"
    if args.series:
        params["series_ticker"] = args.series

    events = get_all_pages(
        base_url       = base_url,
        key_id         = key_id,
        private_key    = private_key,
        path           = "/trade-api/v2/events",
        collection_key = "events",
        params         = params,
    )

    print(f"Fetched {len(events)} events.", file=sys.stderr)
    write_output(events, args, priority_cols=[
        "event_ticker", "series_ticker", "title", "status",
        "open_time", "close_time", "expected_expiration_time",
        "category", "sub_title",
    ])


if __name__ == "__main__":
    main()
