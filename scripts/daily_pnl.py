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

    out_path = OUTPUT_DIR / f"daily_pnl_{pattern}.csv"
    rows = []
    for t in all_tickers:
        c   = cost[t]
        sp  = sell_proceeds[t]
        s   = settle_pnl.get(t, 0.0)
        res = settle_result.get(t, "sold" if sp else "open")
        net = s if sp == 0 else sp - c
        rows.append({"ticker": t, "cost": round(c,4), "sell_proceeds": round(sp,4),
                     "settle_pnl": round(s,4), "net_pnl": round(net,4), "outcome": res})
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker","cost","sell_proceeds","settle_pnl","net_pnl","outcome"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_path}")


def summarise_all() -> None:
    """Group by expiry date extracted from ticker and print one line per day."""
    expiry_re = re.compile(r"\d{2}[A-Z]{3}\d{2}")

    cost_by_expiry    : dict[str, float]       = defaultdict(float)
    pnl_by_expiry     : dict[str, float]       = defaultdict(float)
    yes_by_expiry     : dict[str, int]         = defaultdict(int)
    no_by_expiry      : dict[str, int]         = defaultdict(int)
    pos_by_expiry     : dict[str, int]         = defaultdict(int)
    pos_returns       : dict[str, list[float]] = defaultdict(list)  # per-position pnl/cost

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
                exp  = m.group()
                pnl  = float(row["pnl"])
                cost = float(row.get("cost_basis") or 0)
                pnl_by_expiry[exp] += pnl
                if cost:
                    pos_returns[exp].append(pnl / cost)
                if row["result"] == "yes":
                    yes_by_expiry[exp] += 1
                elif row["result"] == "no":
                    no_by_expiry[exp]  += 1

    all_expiries = sorted(set(cost_by_expiry) | set(pnl_by_expiry))
    if not all_expiries:
        print("No data found. (settlements.csv requires a new run to generate.)")
        return

    print("\n%-10s %6s %8s %8s %5s %5s %6s %9s %9s" % (
        "EXPIRY", "pos", "cost", "net_pnl", "YES", "NO", "win%",
        "cap_ret%", "avg_ret%"))
    print("-" * 80)

    summary_rows = []
    for exp in all_expiries:
        c    = cost_by_expiry[exp]
        pnl  = pnl_by_expiry[exp]
        y    = yes_by_expiry[exp]
        n    = no_by_expiry[exp]
        pos  = pos_by_expiry[exp]
        rets = pos_returns[exp]
        settled    = y + n
        win_pct    = round(y / settled * 100, 1)  if settled else None
        cap_ret    = round(pnl / c * 100, 1)      if c       else None
        avg_ret    = round(sum(rets) / len(rets) * 100, 1) if rets else None
        win_str    = f"{win_pct:.0f}%"   if win_pct is not None else "?"
        cap_str    = f"{cap_ret:+.1f}%"  if cap_ret is not None else "?"
        avg_str    = f"{avg_ret:+.1f}%"  if avg_ret is not None else "?"
        print("%-10s %6d %8.0f %8.0f %5d %5d %6s %9s %9s" % (
            exp, pos, c, pnl, y, n, win_str, cap_str, avg_str))
        summary_rows.append({
            "expiry":     exp,
            "positions":  pos,
            "cost":       round(c, 2),
            "net_pnl":    round(pnl, 2),
            "yes":        y,
            "no":         n,
            "win_pct":    win_pct,
            "cap_ret_pct": cap_ret,
            "avg_ret_pct": avg_ret,
        })

    out_path = OUTPUT_DIR / "daily_pnl.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"\nWrote {out_path}")


def summarise_all_api(pattern: str = "") -> None:
    """Pull fills + market results from the live Kalshi account and summarise."""
    import os
    from collections import defaultdict
    from kalshi_bot.auth import auth_headers, load_private_key
    from dotenv import load_dotenv
    load_dotenv()
    key    = load_private_key(os.environ["KALSHI_API_PRIVATE_KEY_PATH"])
    key_id = os.environ["KALSHI_API_KEY_ID"]
    base   = "https://api.elections.kalshi.com"
    import requests

    def get(path, params=None):
        h = auth_headers(key, key_id, "GET", path)
        return requests.get(base + path, headers=h, params=params, timeout=10).json()

    buy_qty   = defaultdict(float)
    buy_cost  = defaultdict(float)
    sell_qty  = defaultdict(float)
    sell_proc = defaultdict(float)
    expiries  = {}
    import re as _re

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
            if f["action"] == "buy":
                buy_qty[t]  += qty
                buy_cost[t] += qty * price + fee
            else:
                sell_qty[t]  += qty
                sell_proc[t] += qty * price - fee
            if t not in expiries:
                m = _re.search(r"\d{2}[A-Z]{3}\d{2}", t)
                expiries[t] = m.group() if m else "?"
        cursor = data.get("cursor")
        if not cursor or not data.get("fills"):
            break

    # Load results from cache first, only query API for misses
    cache_path = OUTPUT_DIR.parent / "market_results_cache.csv" if OUTPUT_DIR.name != "output" \
                 else OUTPUT_DIR / "market_results_cache.csv"
    # also try default cache location
    default_cache = REPO_ROOT / "output" / "market_results_cache.csv"
    results = {}
    for path in [cache_path, default_cache]:
        if path.exists():
            with path.open() as f:
                for row in csv.DictReader(f):
                    if row["result"] in ("yes", "no", "void"):
                        results[row["ticker"]] = row["result"]
            break

    missing = [t for t in buy_cost if t not in results]
    for ticker in missing:
        try:
            m = get(f"/trade-api/v2/markets/{ticker}").get("market", {})
            r = m.get("result", "")
            if r in ("yes", "no", "void"):
                results[ticker] = r
        except Exception:
            pass

    # Group by expiry
    by_expiry: dict[str, dict] = defaultdict(lambda: {
        "cost": 0.0, "pnl": 0.0, "yes": 0, "no": 0, "pos": 0, "rets": []
    })
    for t, cost in buy_cost.items():
        exp     = expiries.get(t, "?")
        result  = results.get(t, "")
        net_qty = buy_qty[t] - sell_qty.get(t, 0)
        sp      = sell_proc.get(t, 0)
        if result == "yes":
            payout = net_qty * 1.0 + sp
            pnl    = payout - cost
            by_expiry[exp]["yes"] += 1
        elif result == "no":
            payout = sp
            pnl    = payout - cost
            by_expiry[exp]["no"]  += 1
        elif sp > 0 and net_qty <= 0:
            pnl = sp - cost
        else:
            continue
        by_expiry[exp]["cost"] += cost
        by_expiry[exp]["pnl"]  += pnl
        by_expiry[exp]["pos"]  += 1
        if cost:
            by_expiry[exp]["rets"].append(pnl / cost)

    all_exp = sorted(by_expiry)
    if not all_exp:
        print("No settled data found.")
        return

    print("\n%-10s %6s %8s %8s %5s %5s %6s %9s %9s" % (
        "EXPIRY", "pos", "cost", "net_pnl", "YES", "NO", "win%", "cap_ret%", "avg_ret%"))
    print("-" * 82)
    summary_rows = []
    for exp in all_exp:
        d       = by_expiry[exp]
        settled = d["yes"] + d["no"]
        win_pct = round(d["yes"] / settled * 100, 1) if settled else None
        cap_ret = round(d["pnl"] / d["cost"] * 100, 1) if d["cost"] else None
        avg_ret = round(sum(d["rets"]) / len(d["rets"]) * 100, 1) if d["rets"] else None
        print("%-10s %6d %8.0f %8.0f %5d %5d %6s %9s %9s" % (
            exp, d["pos"], d["cost"], d["pnl"],
            d["yes"], d["no"],
            f"{win_pct:.0f}%" if win_pct is not None else "?",
            f"{cap_ret:+.1f}%" if cap_ret is not None else "?",
            f"{avg_ret:+.1f}%" if avg_ret is not None else "?",
        ))
        summary_rows.append({
            "expiry": exp, "positions": d["pos"],
            "cost": round(d["cost"], 2), "net_pnl": round(d["pnl"], 2),
            "yes": d["yes"], "no": d["no"],
            "win_pct": win_pct, "cap_ret_pct": cap_ret, "avg_ret_pct": avg_ret,
        })

    out_path = OUTPUT_DIR / "daily_pnl.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--output-dir" in args:
        idx = args.index("--output-dir")
        OUTPUT_DIR = Path(args[idx + 1])
        args = [a for i, a in enumerate(args) if i != idx and i != idx + 1]
    use_api = "--api" in args
    args    = [a for a in args if a != "--api"]
    pattern = args[0] if args else ""
    if use_api:
        summarise_all_api(pattern)
    elif pattern:
        compute_pnl(pattern)
    else:
        summarise_all()
