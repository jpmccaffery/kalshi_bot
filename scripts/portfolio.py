"""
Print a summary of current Kalshi account: cash balance, open positions,
and exited positions pending settlement.

Usage
-----
    python scripts/portfolio.py           # uses KALSHI_DEMO from .env
    python scripts/portfolio.py --live
    python scripts/portfolio.py --demo
"""

from __future__ import annotations

import sys
from _kalshi_api import base_arg_parser, demo_override_from_args, load_credentials

import requests


def main() -> None:
    parser = base_arg_parser("Show Kalshi account balance and positions")
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

    # Balance
    balance_cents = get("/trade-api/v2/portfolio/balance").get("balance", 0)
    balance       = balance_cents / 100.0

    # All positions — no count_filter so we also get exited-but-unsettled ones
    all_positions = get(
        "/trade-api/v2/portfolio/positions",
        params={"limit": 1000},
    ).get("market_positions", [])

    open_pos     = [p for p in all_positions if float(p.get("position_fp", 0) or 0) > 0]
    unsettled    = [p for p in all_positions
                    if float(p.get("position_fp", 0) or 0) == 0
                    and float(p.get("realized_pnl_dollars", 0) or 0) != 0]

    total_exposure   = sum(float(p.get("market_exposure_dollars", 0) or 0) for p in open_pos)
    pending_pnl      = sum(float(p.get("realized_pnl_dollars",    0) or 0) for p in unsettled)

    print(f"\n{'='*60}")
    print(f"  Cash balance        : ${balance:>12,.4f}")
    print(f"  Open positions      : {len(open_pos):>3}  (exposure ${total_exposure:,.4f})")
    print(f"  Unsettled exits     : {len(unsettled):>3}  (pending P&L ${pending_pnl:+,.4f})")
    print(f"  Est. total value    : ${balance + total_exposure + pending_pnl:>12,.4f}")
    print(f"{'='*60}")

    if open_pos:
        print(f"\n  OPEN POSITIONS")
        print(f"  {'Ticker':<35} {'Qty':>6}  {'Avg Entry':>9}  {'Exposure':>10}  {'Realized P&L':>12}")
        print(f"  {'-'*35} {'-'*6}  {'-'*9}  {'-'*10}  {'-'*12}")
        for p in sorted(open_pos, key=lambda x: x.get("ticker", "")):
            ticker   = p.get("ticker", "?")
            qty      = float(p.get("position_fp", 0) or 0)
            exposure = float(p.get("market_exposure_dollars", 0) or 0)
            avg      = (exposure / qty) if qty else 0.0
            rpnl     = float(p.get("realized_pnl_dollars", 0) or 0)
            print(f"  {ticker:<35} {qty:>6.0f}  {avg:>9.4f}  ${exposure:>9.4f}  ${rpnl:>+11.4f}")

    if unsettled:
        print(f"\n  EXITED — PENDING SETTLEMENT")
        print(f"  {'Ticker':<35} {'Traded':>10}  {'Realized P&L':>12}  {'Fees':>8}  {'Last Updated'}")
        print(f"  {'-'*35} {'-'*10}  {'-'*12}  {'-'*8}  {'-'*20}")
        for p in sorted(unsettled, key=lambda x: x.get("ticker", "")):
            ticker  = p.get("ticker", "?")
            traded  = float(p.get("total_traded_dollars", 0) or 0)
            rpnl    = float(p.get("realized_pnl_dollars", 0) or 0)
            fees    = float(p.get("fees_paid_dollars", 0) or 0)
            updated = (p.get("last_updated_ts") or "")[:19].replace("T", " ")
            print(f"  {ticker:<35} ${traded:>9.4f}  ${rpnl:>+11.4f}  ${fees:>7.4f}  {updated}")

    if not open_pos and not unsettled:
        print("  No positions.")

    print()


if __name__ == "__main__":
    main()
