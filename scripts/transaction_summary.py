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
            result = s.get("result", "")
            if s.get("payout"):
                # Paper client writes full payout — use it directly (handles NO positions)
                proceeds = float(s["payout"])
            else:
                # Live client writes only result — assume YES position (all current live trades)
                proceeds = entry["quantity"] if result == "yes" else 0.0
            pnl        = proceeds - cost
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


def load_from_api(pattern: str) -> list[dict]:
    """Pull per-position detail from the live Kalshi account via the fills API."""
    import os, re as _re, requests
    from collections import defaultdict
    from kalshi_bot.auth import auth_headers, load_private_key
    from dotenv import load_dotenv
    load_dotenv()

    key    = load_private_key(os.environ["KALSHI_API_PRIVATE_KEY_PATH"])
    key_id = os.environ["KALSHI_API_KEY_ID"]
    base   = "https://api.elections.kalshi.com"

    def get(path, params=None):
        h = auth_headers(key, key_id, "GET", path)
        return requests.get(base + path, headers=h, params=params, timeout=10).json()

    buy_qty   = defaultdict(float)
    buy_cost  = defaultdict(float)
    buy_fees  = defaultdict(float)
    buy_first = {}          # ticker → first fill datetime string
    sell_qty  = defaultdict(float)
    sell_proc = defaultdict(float)
    sell_fees = defaultdict(float)
    sell_last = {}          # ticker → last sell datetime

    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = get("/trade-api/v2/portfolio/fills", params)
        for f in data.get("fills", []):
            t = f["ticker"]
            if pattern and pattern not in t:
                continue
            qty   = float(f["count_fp"])
            price = float(f["yes_price_dollars"])
            fee   = float(f["fee_cost"])
            ts    = f["created_time"][:16].replace("T", " ")
            if f["action"] == "buy":
                if t not in buy_first:
                    buy_first[t] = ts
                buy_qty[t]  += qty
                buy_cost[t] += qty * price
                buy_fees[t] += fee
            else:
                sell_qty[t]  += qty
                sell_proc[t] += qty * price
                sell_fees[t] += fee
                sell_last[t]  = ts
        cursor = data.get("cursor")
        if not cursor or not data.get("fills"):
            break

    # Get market results from cache then API for misses
    cache_path = REPO_ROOT / "output" / "market_results_cache.csv"
    results: dict[str, str] = {}
    close_times: dict[str, str] = {}
    if cache_path.exists():
        with cache_path.open() as f:
            for row in csv.DictReader(f):
                if row["result"] in ("yes", "no", "void"):
                    results[row["ticker"]] = row["result"]
                    close_times[row["ticker"]] = row.get("ts", "")

    for ticker in buy_qty:
        if ticker not in results:
            try:
                m = get(f"/trade-api/v2/markets/{ticker}").get("market", {})
                r = m.get("result", "")
                if r in ("yes", "no", "void"):
                    results[ticker] = r
                    close_times[ticker] = (m.get("close_time") or "")[:16].replace("T", " ")
            except Exception:
                pass

    rows = []
    for t in sorted(buy_qty):
        cost      = buy_cost[t] + buy_fees[t]
        bqty      = buy_qty[t]
        sqty      = sell_qty.get(t, 0)
        net_qty   = bqty - sqty
        sp        = sell_proc.get(t, 0) - sell_fees.get(t, 0)
        result    = results.get(t, "")
        expiry    = _expiry(t)

        if result == "yes":
            payout    = net_qty * 1.0 + sp
            pnl       = round(payout - cost, 4)
            exit_type = "settled_yes"
            exit_date = close_times.get(t, "")
            exit_price = round(payout / bqty, 6) if bqty else ""
        elif result == "no":
            payout    = sp
            pnl       = round(payout - cost, 4)
            exit_type = "settled_no"
            exit_date = close_times.get(t, "")
            exit_price = round(payout / bqty, 6) if bqty else ""
        elif sqty > 0 and net_qty <= 0:
            payout    = sp
            pnl       = round(payout - cost, 4)
            exit_type = "sold"
            exit_date = sell_last.get(t, "")
            exit_price = round(sp / sqty, 6) if sqty else ""
        else:
            pnl = payout = ""
            exit_type  = "open"
            exit_date  = ""
            exit_price = ""

        ret_pct = round(pnl / cost * 100, 2) if (pnl != "" and cost) else ""
        avg_buy  = round(buy_cost[t] / bqty, 6) if bqty else ""

        rows.append({
            "ticker":     t,
            "expiry":     expiry,
            "buy_date":   buy_first.get(t, ""),
            "buy_price":  avg_buy,
            "quantity":   round(bqty, 4),
            "cost":       round(cost, 4),
            "model_prob": "",
            "edge":       "",
            "days_out":   "",
            "exit_date":  exit_date,
            "exit_type":  exit_type,
            "exit_price": exit_price,
            "proceeds":   round(payout, 4) if payout != "" else "",
            "pnl":        pnl,
            "return_pct": ret_pct,
        })

    return rows


def main() -> None:
    use_all  = "--all" in sys.argv
    use_api  = "--api" in sys.argv
    sort_by  = "buy_date"
    out_dir  = OUTPUT_DIR
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
        elif a == "--output-dir":
            if i + 1 < len(sys.argv):
                out_dir = Path(sys.argv[i + 1])
                skip_next = True
        elif not a.startswith("--"):
            positional.append(a)
    pattern = positional[0] if positional else ""

    if use_api:
        print("Fetching from Kalshi API...")
        rows = load_from_api(pattern)
    else:
        if use_all:
            run_dirs = sorted(out_dir.glob("run_*"))
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
    out_path = out_dir / f"transaction_summary{suffix}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows → {out_path}")

    settled   = [r for r in rows if "settled" in r.get("exit_type", "")]
    sold      = [r for r in rows if r.get("exit_type") == "sold"]
    open_     = [r for r in rows if r.get("exit_type") == "open"]
    total_pnl = sum(float(r["pnl"]) for r in rows if r.get("pnl") != "")
    print(f"Settled: {len(settled)}  Sold early: {len(sold)}  Open: {len(open_)}")
    print(f"Total PnL on closed positions: ${total_pnl:+.2f}")


if __name__ == "__main__":
    main()
