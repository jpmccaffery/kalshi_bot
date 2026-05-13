"""
Show unrealized P&L for each open position by comparing avg entry price
against current yes_bid from the market API.

Usage
-----
    python scripts/position_pnl.py
    python scripts/position_pnl.py --live
"""

from __future__ import annotations

import sys
from _kalshi_api import base_arg_parser, demo_override_from_args, load_credentials

import requests


def main() -> None:
    parser = base_arg_parser("Show unrealized P&L for open positions")
    args   = parser.parse_args()

    key_id, private_key, base_url = load_credentials(
        demo_override=demo_override_from_args(args)
    )

    from kalshi_bot.auth import auth_headers

    def get(path: str, params: dict | None = None) -> dict:
        headers = auth_headers(private_key, key_id, "GET", path)
        resp    = requests.get(base_url + path, headers=headers,
                               params=params or {}, timeout=10)
        if resp.status_code != 200:
            sys.exit(f"API error {resp.status_code} on {path}: {resp.text}")
        return resp.json()

    positions = get("/trade-api/v2/portfolio/positions", {"limit": 1000}).get("market_positions", [])
    open_pos  = [p for p in positions if float(p.get("position_fp", 0) or 0) > 0]

    if not open_pos:
        print("No open positions.")
        return

    print(f"\n  {'Ticker':<35} {'Qty':>6}  {'Avg Entry':>9}  {'Yes Bid':>7}  {'Yes Ask':>7}  {'Unreal P&L':>10}  {'P&L %':>7}")
    print(f"  {'-'*35} {'-'*6}  {'-'*9}  {'-'*7}  {'-'*7}  {'-'*10}  {'-'*7}")

    total_cost = 0.0
    total_value = 0.0

    for p in sorted(open_pos, key=lambda x: x.get("ticker", "")):
        ticker   = p.get("ticker", "?")
        qty      = float(p.get("position_fp", 0) or 0)
        exposure = float(p.get("market_exposure_dollars", 0) or 0)
        avg      = exposure / qty if qty else 0.0

        # Fetch current market price
        mkt_data = get(f"/trade-api/v2/markets/{ticker}").get("market", {})
        yes_bid  = float(mkt_data.get("yes_bid_dollars", 0) or 0)
        yes_ask  = float(mkt_data.get("yes_ask_dollars", 0) or 0)

        if yes_bid > 0 and avg > 0:
            unreal_pnl = (yes_bid - avg) * qty
            pnl_pct    = (yes_bid - avg) / avg * 100
        else:
            unreal_pnl = float("nan")
            pnl_pct    = float("nan")

        pnl_str = f"${unreal_pnl:>+9.4f}" if unreal_pnl == unreal_pnl else "       n/a"
        pct_str = f"{pnl_pct:>+6.1f}%" if pnl_pct == pnl_pct else "    n/a"

        print(f"  {ticker:<35} {qty:>6.0f}  ${avg:>8.4f}  ${yes_bid:>6.4f}  ${yes_ask:>6.4f}  {pnl_str}  {pct_str}")

        total_cost  += exposure
        if yes_bid > 0:
            total_value += yes_bid * qty

    print(f"\n  Total cost basis : ${total_cost:,.4f}")
    print(f"  Total mkt value  : ${total_value:,.4f}  (bid-side)")
    print(f"  Unrealized P&L   : ${total_value - total_cost:+,.4f}\n")


if __name__ == "__main__":
    main()
