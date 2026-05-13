"""
Produce a per-ticker transaction summary.

Defaults to the latest run only. Use --all to include all runs.

Usage:
    python scripts/transaction_summary.py                       # latest run, all tickers
    python scripts/transaction_summary.py 26MAY09               # latest run, filter by expiry
    python scripts/transaction_summary.py 26MAY09 --sort pnl   # sort by pnl
    python scripts/transaction_summary.py --all                 # all runs
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "output"


def latest_run() -> Path:
    runs = sorted(OUTPUT_DIR.glob("run_*"), reverse=True)
    if not runs:
        raise FileNotFoundError(f"No run directories in {OUTPUT_DIR}")
    return runs[0]

FIELDNAMES = [
    "ticker", "expiry",
    "buy_date", "buy_price", "quantity", "cost",
    "model_prob", "edge", "days_out",
    "exit_date", "exit_type", "exit_price", "proceeds",
    "pnl", "return_pct",
]


def load_all(pattern: str, run_dirs: list[Path]) -> list[dict]:
    # --- buys ---
    buys: dict[str, dict] = {}
    signals: dict[tuple, dict] = {}  # (date, symbol) -> signal row

    for run_dir in run_dirs:
        sig_path = run_dir / "signals.csv"
        if sig_path.exists():
            with sig_path.open() as f:
                for row in csv.DictReader(f):
                    key = (row["date"], row["symbol"])
                    signals[key] = row

    for run_dir in run_dirs:
        orders_path = run_dir / "orders.csv"
        if not orders_path.exists():
            continue
        with orders_path.open() as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "")
                if pattern and pattern not in sym:
                    continue
                if row["side"] != "buy":
                    continue
                qty   = float(row.get("filled_qty") or 0)
                price = float(row.get("fill_price") or 0)
                if qty <= 0:
                    continue
                sig = signals.get((row["date"], sym), {})
                entry = {
                    "ticker":     sym,
                    "expiry":     _expiry(sym),
                    "buy_date":   row["date"],
                    "buy_price":  price,
                    "quantity":   qty,
                    "cost":       round(qty * price, 4),
                    "model_prob": _f(sig.get("meta_model_prob")),
                    "edge":       _f(sig.get("meta_edge") or sig.get("edge")),
                    "days_out":   _f(sig.get("meta_days_out")),
                    "exit_date":  "",
                    "exit_type":  "open",
                    "exit_price": "",
                    "proceeds":   "",
                    "pnl":        "",
                    "return_pct": "",
                }
                if sym in buys:
                    prev = buys[sym]
                    total_qty  = prev["quantity"] + qty
                    total_cost = prev["cost"] + entry["cost"]
                    prev["quantity"]  = total_qty
                    prev["cost"]      = round(total_cost, 4)
                    prev["buy_price"] = round(total_cost / total_qty, 6)
                else:
                    buys[sym] = entry

    # --- early sells ---
    sell_proceeds: dict[str, float] = defaultdict(float)
    sell_dates:    dict[str, str]   = {}
    for run_dir in run_dirs:
        orders_path = run_dir / "orders.csv"
        if not orders_path.exists():
            continue
        with orders_path.open() as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "")
                if pattern and pattern not in sym:
                    continue
                if row["side"] != "sell":
                    continue
                qty   = float(row.get("filled_qty") or 0)
                price = float(row.get("fill_price") or 0)
                sell_proceeds[sym] += qty * price
                sell_dates[sym]     = row["date"]

    # --- settlements ---
    settlements: dict[str, dict] = {}
    for run_dir in run_dirs:
        settle_path = run_dir / "settlements.csv"
        if not settle_path.exists():
            continue
        with settle_path.open() as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "")
                if pattern and pattern not in sym:
                    continue
                settlements[sym] = row

    # --- merge ---
    rows = []
    for sym, entry in buys.items():
        cost = entry["cost"]
        sp   = sell_proceeds.get(sym, 0.0)
        s    = settlements.get(sym)

        if s:
            pnl        = float(s["pnl"])
            proceeds   = float(s["payout"])
            exit_price = round(proceeds / entry["quantity"], 6) if entry["quantity"] else ""
            entry.update({
                "exit_date":  s["ts"],
                "exit_type":  f"settled_{s['result']}",
                "exit_price": exit_price,
                "proceeds":   round(proceeds, 4),
                "pnl":        round(pnl, 4),
                "return_pct": round(pnl / cost * 100, 2) if cost else "",
            })
        elif sp > 0:
            pnl = sp - cost
            entry.update({
                "exit_date":  sell_dates.get(sym, ""),
                "exit_type":  "sold",
                "exit_price": round(sp / entry["quantity"], 6) if entry["quantity"] else "",
                "proceeds":   round(sp, 4),
                "pnl":        round(pnl, 4),
                "return_pct": round(pnl / cost * 100, 2) if cost else "",
            })

        rows.append(entry)

    return rows


def _expiry(sym: str) -> str:
    import re
    m = re.search(r"\d{2}[A-Z]{3}\d{2}", sym)
    return m.group() if m else ""


def _f(v):
    if v is None or v == "":
        return ""
    try:
        return float(v)
    except (ValueError, TypeError):
        return v


def main() -> None:
    use_all = "--all" in sys.argv
    sort_by = "buy_date"
    skip_next = False
    positional = []
    for i, a in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if a == "--sort":
            if i + 1 < len(sys.argv):
                sort_by = sys.argv[i + 1]
                skip_next = True
        elif not a.startswith("--"):
            positional.append(a)
    pattern = positional[0] if positional else ""

    if use_all:
        run_dirs = sorted(OUTPUT_DIR.glob("run_*"))
        label    = "all runs"
    else:
        run_dirs = [latest_run()]
        label    = run_dirs[0].name
    print(f"Using: {label}")

    rows = load_all(pattern, run_dirs)
    if not rows:
        print("No data found.")
        return

    # Sort
    def sort_key(r):
        v = r.get(sort_by, "")
        if v == "":
            return (1, 0)
        try:
            return (0, float(v))
        except (ValueError, TypeError):
            return (0, str(v))

    rows.sort(key=sort_key)

    suffix   = f"_{pattern}" if pattern else ""
    out_path = OUTPUT_DIR / f"transaction_summary{suffix}.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows → {out_path}")

    # Quick summary
    settled = [r for r in rows if "settled" in r.get("exit_type", "")]
    sold    = [r for r in rows if r.get("exit_type") == "sold"]
    open_   = [r for r in rows if r.get("exit_type") == "open"]
    total_pnl = sum(float(r["pnl"]) for r in rows if r.get("pnl") != "")
    print(f"Settled: {len(settled)}  Sold early: {len(sold)}  Open: {len(open_)}")
    print(f"Total PnL on closed positions: ${total_pnl:+.2f}")


if __name__ == "__main__":
    main()
