"""
Compute net PnL for all markets matching an expiry pattern across all runs.

Usage:
    python scripts/daily_pnl.py 26MAY06
    python scripts/daily_pnl.py 26MAY          # all May 2026 markets
    python scripts/daily_pnl.py                # summarise every expiry date found
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "output"


def load_orders(pattern: str) -> list[dict]:
    rows = []
    for path in sorted(OUTPUT_DIR.glob("run_*/orders.csv")):
        with path.open() as f:
            for row in csv.DictReader(f):
                if pattern in row.get("symbol", ""):
                    row["_run"] = path.parent.name
                    rows.append(row)
    return rows


def load_settlements(pattern: str) -> list[dict]:
    rows = []
    for path in sorted(OUTPUT_DIR.glob("run_*/settlements.csv")):
        with path.open() as f:
            for row in csv.DictReader(f):
                if pattern in row.get("symbol", ""):
                    row["_run"] = path.parent.name
                    rows.append(row)
    return rows


def compute_pnl(pattern: str) -> None:
    orders      = load_orders(pattern)
    settlements = load_settlements(pattern)

    cost          : dict[str, float] = defaultdict(float)
    sell_proceeds : dict[str, float] = defaultdict(float)
    settle_pnl    : dict[str, float] = {}
    settle_result : dict[str, str]   = {}

    for r in orders:
        sym   = r["symbol"]
        qty   = float(r.get("filled_qty") or 0)
        price = float(r.get("fill_price") or 0)
        if r["side"] == "buy":
            cost[sym] += qty * price
        elif r["side"] == "sell":
            sell_proceeds[sym] += qty * price

    for r in settlements:
        sym = r["symbol"]
        settle_pnl[sym]    = float(r["pnl"])
        settle_result[sym] = r["result"]

    all_tickers = sorted(
        set(cost) | set(sell_proceeds) | set(settle_pnl)
    )

    if not all_tickers:
        print(f"No data found for pattern '{pattern}'")
        return

    print(f"\nPattern: {pattern}  ({len(all_tickers)} positions)\n")
    print("%-45s %8s %8s %8s %8s  %s" % (
        "TICKER", "cost", "sell$", "settle", "net_pnl", "outcome"))
    print("-" * 105)

    total_cost = total_sell = total_settle = total_net = 0.0
    yes = no = void = sold = 0

    for t in all_tickers:
        c   = cost[t]
        sp  = sell_proceeds[t]
        s   = settle_pnl.get(t, 0.0)
        res = settle_result.get(t, "sold" if sp else "open")
        # settle_pnl is already net of cost; sold proceeds need cost deducted
        net = s if sp == 0 else sp - c
        total_cost    += c
        total_sell    += sp
        total_settle  += s
        total_net     += net
        if res == "yes":   yes  += 1
        elif res == "no":  no   += 1
        elif res == "void":void += 1
        else:              sold += 1
        print("%-45s %8.2f %8.2f %8.2f %8.2f  %s" % (t, c, sp, s, net, res))

    print("-" * 105)
    print("%-45s %8.2f %8.2f %8.2f %8.2f" % (
        "TOTAL", total_cost, total_sell, total_settle, total_net))
    print()
    settled = yes + no + void
    print(f"YES: {yes}  NO: {no}  VOID: {void}  Sold early: {sold}  Open: {len(all_tickers)-settled-sold}")
    if settled:
        print(f"Win rate (settled): {yes/settled*100:.0f}%")
    print(f"Return on deployed: {total_net/total_cost*100:+.1f}%")


def summarise_all() -> None:
    """Group by expiry date extracted from ticker and print one line per day."""
    expiry_re = re.compile(r"\d{2}[A-Z]{3}\d{2}")

    cost_by_expiry : dict[str, float] = defaultdict(float)
    pnl_by_expiry  : dict[str, float] = defaultdict(float)
    yes_by_expiry  : dict[str, int]   = defaultdict(int)
    no_by_expiry   : dict[str, int]   = defaultdict(int)
    pos_by_expiry  : dict[str, int]   = defaultdict(int)

    for path in sorted(OUTPUT_DIR.glob("run_*/orders.csv")):
        with path.open() as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "")
                m   = expiry_re.search(sym)
                if not m:
                    continue
                exp = m.group()
                qty   = float(row.get("filled_qty") or 0)
                price = float(row.get("fill_price") or 0)
                if row["side"] == "buy":
                    cost_by_expiry[exp] += qty * price
                    pos_by_expiry[exp]  += 1
                elif row["side"] == "sell":
                    pnl_by_expiry[exp]  += qty * price - qty * price  # nets to 0 here

    for path in sorted(OUTPUT_DIR.glob("run_*/settlements.csv")):
        with path.open() as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "")
                m   = expiry_re.search(sym)
                if not m:
                    continue
                exp = m.group()
                pnl_by_expiry[exp] += float(row["pnl"])
                if row["result"] == "yes":
                    yes_by_expiry[exp] += 1
                elif row["result"] == "no":
                    no_by_expiry[exp]  += 1

    all_expiries = sorted(set(cost_by_expiry) | set(pnl_by_expiry))
    if not all_expiries:
        print("No data found. (settlements.csv requires a new run to generate.)")
        return

    print("\n%-10s %6s %8s %8s %5s %5s %6s %8s" % (
        "EXPIRY", "pos", "cost", "net_pnl", "YES", "NO", "win%", "return%"))
    print("-" * 70)

    summary_rows = []
    for exp in all_expiries:
        c    = cost_by_expiry[exp]
        pnl  = pnl_by_expiry[exp]
        y    = yes_by_expiry[exp]
        n    = no_by_expiry[exp]
        pos  = pos_by_expiry[exp]
        settled = y + n
        win_pct    = round(y / settled * 100, 1) if settled else None
        return_pct = round(pnl / c * 100, 1)     if c       else None
        win_str    = f"{win_pct:.0f}%" if win_pct is not None else "?"
        ret_str    = f"{return_pct:+.1f}%" if return_pct is not None else "?"
        print("%-10s %6d %8.0f %8.0f %5d %5d %6s %8s" % (
            exp, pos, c, pnl, y, n, win_str, ret_str))
        summary_rows.append({
            "expiry":     exp,
            "positions":  pos,
            "cost":       round(c, 2),
            "net_pnl":    round(pnl, 2),
            "yes":        y,
            "no":         n,
            "win_pct":    win_pct,
            "return_pct": return_pct,
        })

    out_path = OUTPUT_DIR / "daily_pnl.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        compute_pnl(sys.argv[1])
    else:
        summarise_all()
