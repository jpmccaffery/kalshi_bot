#!/usr/bin/env python3
"""
List all open Kalshi markets (optionally filtered by event or series).

A market is the tradeable contract — it has a yes_bid/yes_ask and a
binary outcome. This is what gets passed to KalshiDataFeed as a symbol.

Usage
-----
    python scripts/list_markets.py
    python scripts/list_markets.py --series KXINX
    python scripts/list_markets.py --event KXINX-25JAN15
    python scripts/list_markets.py --output markets.csv
    python scripts/list_markets.py --live --series KXINX --output markets_live.csv

    # Just print tickers, one per line (useful for KALSHI_MARKETS env var):
    python scripts/list_markets.py --series KXINX --tickers-only
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
    parser = base_arg_parser("Fetch all open Kalshi markets.")
    parser.add_argument(
        "--series", "-s",
        metavar="TICKER",
        help="Filter to markets belonging to this series (e.g. KXINX).",
    )
    parser.add_argument(
        "--event", "-e",
        metavar="TICKER",
        help="Filter to markets belonging to this event (e.g. KXINX-25JAN15).",
    )
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Include closed/settled markets, not just open ones.",
    )
    parser.add_argument(
        "--tickers-only",
        action="store_true",
        help="Print one ticker per line instead of full JSON (ignores --output).",
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
    if args.event:
        params["event_ticker"] = args.event

    markets = get_all_pages(
        base_url       = base_url,
        key_id         = key_id,
        private_key    = private_key,
        path           = "/trade-api/v2/markets",
        collection_key = "markets",
        params         = params,
    )

    print(f"Fetched {len(markets)} markets.", file=sys.stderr)

    if args.tickers_only:
        for m in markets:
            print(m.get("ticker", ""))
        return

    write_output(markets, args, priority_cols=[
        "ticker", "event_ticker", "series_ticker", "title", "status",
        "yes_bid", "yes_ask", "no_bid", "no_ask",
        "volume", "open_interest",
        "open_time", "close_time", "expected_expiration_time",
        "sub_title",
    ])


if __name__ == "__main__":
    main()
