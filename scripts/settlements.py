"""
Print recent Kalshi settlement history.

Usage
-----
    python scripts/settlements.py           # uses KALSHI_DEMO from .env
    python scripts/settlements.py --live
    python scripts/settlements.py --demo
    python scripts/settlements.py --ticker KXWTI-26APR17-T93.99
"""

from __future__ import annotations

import sys
import argparse
from _kalshi_api import base_arg_parser, demo_override_from_args, load_credentials

import requests


def main() -> None:
    parser = base_arg_parser("Show Kalshi settlement history")
    parser.add_argument("--ticker", help="Filter to a specific market ticker")
    args   = parser.parse_args()

    key_id, private_key, base_url = load_credentials(
        demo_override=demo_override_from_args(args)
    )

    from kalshi_bot.auth import auth_headers

    path    = "/trade-api/v2/portfolio/settlements"
    params  = {"limit": 100}
    if args.ticker:
        params["ticker"] = args.ticker

    headers = auth_headers(private_key, key_id, "GET", path)
    resp    = requests.get(base_url + path, headers=headers, params=params, timeout=10)
    if resp.status_code != 200:
        sys.exit(f"API error {resp.status_code}: {resp.text}")

    settlements = resp.json().get("settlements", [])

    if not settlements:
        print("No settlements found.")
        return

    print(f"\n  {'Ticker':<35} {'Result':<6} {'Yes Qty':>7}  {'Cost Basis':>10}  {'Revenue':>10}  {'Net P&L':>10}  {'Settled At'}")
    print(f"  {'-'*35} {'-'*6} {'-'*7}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*20}")

    total_pnl = 0.0
    for s in settlements:
        ticker     = s.get("ticker", "?")
        result     = s.get("market_result", "?")
        yes_qty    = float(s.get("yes_count_fp", 0) or 0)
        cost       = float(s.get("yes_total_cost_dollars", 0) or 0)
        revenue    = float(s.get("revenue", 0) or 0) / 100.0  # revenue is in cents
        fee        = float(s.get("fee_cost", 0) or 0)
        net_pnl    = revenue - cost - fee
        settled_at = s.get("settled_time", "")[:19].replace("T", " ")
        total_pnl += net_pnl

        print(
            f"  {ticker:<35} {result:<6} {yes_qty:>7.0f}  "
            f"${cost:>9.4f}  ${revenue:>9.4f}  ${net_pnl:>+9.4f}  {settled_at}"
        )

    print(f"\n  Total net P&L across {len(settlements)} settlement(s): ${total_pnl:+.4f}\n")


if __name__ == "__main__":
    main()
