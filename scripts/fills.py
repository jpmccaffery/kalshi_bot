"""
Print recent Kalshi fills (executed trades).

Usage
-----
    python scripts/fills.py                        # last 100 fills
    python scripts/fills.py --ticker KXWTI-26APR17-T93.99
    python scripts/fills.py --live
"""

from __future__ import annotations

import sys
import argparse
from _kalshi_api import base_arg_parser, demo_override_from_args, load_credentials

import requests


def main() -> None:
    parser = base_arg_parser("Show recent Kalshi fills")
    parser.add_argument("--ticker", help="Filter to a specific market ticker")
    parser.add_argument("--limit", type=int, default=50, help="Number of fills to show (default 50)")
    args   = parser.parse_args()

    key_id, private_key, base_url = load_credentials(
        demo_override=demo_override_from_args(args)
    )

    from kalshi_bot.auth import auth_headers

    path   = "/trade-api/v2/portfolio/fills"
    params = {"limit": args.limit}
    if args.ticker:
        params["ticker"] = args.ticker

    headers = auth_headers(private_key, key_id, "GET", path)
    resp    = requests.get(base_url + path, headers=headers, params=params, timeout=10)
    if resp.status_code != 200:
        sys.exit(f"API error {resp.status_code}: {resp.text}")

    fills = resp.json().get("fills", [])

    if not fills:
        print("No fills found.")
        return

    print(f"\n  {'Ticker':<35} {'Action':<5} {'Side':<4} {'Qty':>6}  {'Price':>7}  {'Fee':>7}  {'Time'}")
    print(f"  {'-'*35} {'-'*5} {'-'*4} {'-'*6}  {'-'*7}  {'-'*7}  {'-'*20}")

    for f in fills:
        ticker  = f.get("ticker", f.get("market_ticker", "?"))
        action  = f.get("action", "?")
        side    = f.get("side", "?")
        qty     = float(f.get("count_fp", 0) or 0)
        price   = float(f.get("yes_price_dollars", 0) or 0)
        fee     = float(f.get("fee_cost", 0) or 0)
        ts      = (f.get("created_time") or "")[:19].replace("T", " ")
        print(f"  {ticker:<35} {action:<5} {side:<4} {qty:>6.0f}  ${price:>6.4f}  ${fee:>6.4f}  {ts}")

    print(f"\n  {len(fills)} fill(s) shown.\n")


if __name__ == "__main__":
    main()
