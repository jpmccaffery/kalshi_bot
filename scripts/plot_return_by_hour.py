"""
Scatter + mean bar chart of position return % vs hour of purchase.

Usage:
    python scripts/plot_return_by_hour.py                          # latest run (CSV)
    python scripts/plot_return_by_hour.py output/run_20260507_205156
    python scripts/plot_return_by_hour.py --api                    # live account via Kalshi API
    python scripts/plot_return_by_hour.py --api --out output/live  # save plot to custom dir
    python scripts/plot_return_by_hour.py --compare output/run_20260507_205156  # paper vs live
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT  = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "output"


def latest_run() -> Path:
    runs = sorted(OUTPUT_DIR.glob("run_*"), reverse=True)
    if not runs:
        raise FileNotFoundError("No run directories found")
    return runs[0]


def load(run_dirs: Path | list[Path]) -> list[dict]:
    """Return list of {symbol, hour, return_pct, outcome} for all closed positions.
    Accepts a single run directory or a list of them."""

    if isinstance(run_dirs, Path):
        run_dirs = [run_dirs]

    # --- buys: first buy time and cost per symbol ---
    first_buy_ts: dict[str, str]   = {}
    buy_cost:     dict[str, float] = defaultdict(float)
    buy_qty:      dict[str, float] = defaultdict(float)

    for run_dir in run_dirs:
        with (run_dir / "orders.csv").open() as f:
            for row in csv.DictReader(f):
                if row["side"] != "buy":
                    continue
                qty   = float(row.get("filled_qty") or 0)
                price = float(row.get("fill_price") or 0)
                if qty <= 0 or price <= 0:
                    continue
                sym = row["symbol"]
                if sym not in first_buy_ts:
                    first_buy_ts[sym] = row["date"]
                buy_cost[sym] += qty * price
                buy_qty[sym]  += qty

    # --- early sells ---
    sell_proceeds: dict[str, float] = defaultdict(float)
    sell_qty:      dict[str, float] = defaultdict(float)
    for run_dir in run_dirs:
        with (run_dir / "orders.csv").open() as f:
            for row in csv.DictReader(f):
                if row["side"] != "sell":
                    continue
                qty   = float(row.get("filled_qty") or 0)
                price = float(row.get("fill_price") or 0)
                if qty <= 0 or price <= 0:
                    continue
                sym = row["symbol"]
                sell_proceeds[sym] += qty * price
                sell_qty[sym]      += qty

    # --- settlements ---
    settlements: dict[str, dict] = {}
    for run_dir in run_dirs:
        settle_path = run_dir / "settlements.csv"
        if settle_path.exists():
            with settle_path.open() as f:
                for row in csv.DictReader(f):
                    settlements[row["symbol"]] = row

    # --- build result rows ---
    rows = []
    for sym, ts in first_buy_ts.items():
        cost = buy_cost[sym]
        if cost <= 0:
            continue

        # parse hour from "YYYY-MM-DD HH:MM"
        hour = int(ts[11:13])

        if sym in settlements:
            s   = settlements[sym]
            pnl = float(s["pnl"])
            outcome = f"settled_{s['result']}"
        elif sell_proceeds.get(sym, 0) > 0:
            pnl     = sell_proceeds[sym] - cost
            outcome = "sold"
        else:
            continue  # still open — skip

        rows.append({
            "symbol":     sym,
            "hour":       hour,
            "return_pct": pnl / cost * 100,
            "outcome":    outcome,
            "pnl":        pnl,
        })

    return rows


def load_from_api() -> list[dict]:
    """Fetch fills + market results from Kalshi API (for live account)."""
    from kalshi_bot.auth import auth_headers, load_private_key
    from dotenv import load_dotenv
    import os, requests

    load_dotenv()
    key    = load_private_key(os.environ["KALSHI_API_PRIVATE_KEY_PATH"])
    key_id = os.environ["KALSHI_API_KEY_ID"]
    base   = "https://api.elections.kalshi.com"

    def get(path, params=None):
        h = auth_headers(key, key_id, "GET", path)
        return requests.get(base + path, headers=h, params=params, timeout=10).json()

    # Collect all fills
    buy_first_ts:  dict[str, str]   = {}
    buy_cost:      dict[str, float] = defaultdict(float)
    sell_proceeds: dict[str, float] = defaultdict(float)

    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = get("/trade-api/v2/portfolio/fills", params)
        for f in data.get("fills", []):
            t     = f["ticker"]
            qty   = float(f["count_fp"])
            price = float(f["yes_price_dollars"])
            fee   = float(f["fee_cost"])
            ts    = f["created_time"][:13].replace("T", " ")  # "YYYY-MM-DD HH"
            if f["action"] == "buy":
                if t not in buy_first_ts:
                    buy_first_ts[t] = ts
                buy_cost[t] += qty * price + fee
            else:
                sell_proceeds[t] += qty * price - fee
        cursor = data.get("cursor")
        if not cursor or not data.get("fills"):
            break

    # Fetch market results for all buy tickers
    results: dict[str, str] = {}
    for ticker in buy_cost:
        m = get(f"/trade-api/v2/markets/{ticker}").get("market", {})
        results[ticker] = m.get("result", "")

    rows = []
    for sym, ts in buy_first_ts.items():
        cost = buy_cost[sym]
        if cost <= 0:
            continue
        hour   = int(ts[11:13])
        result = results.get(sym, "")
        net    = buy_cost[sym] - sell_proceeds.get(sym, 0)  # net qty value remaining

        if result == "yes":
            qty_held = buy_cost[sym] / (buy_cost[sym] / max(buy_cost[sym], 0.0001))
            # payout = contracts_held × $1; approximate from cost at avg price
            # Use: pnl = settlement_payout - all_in_cost
            # settlement_payout for YES = net_qty × $1; get net_qty from fills
            pnl     = float("nan")  # recalculate below
            outcome = "settled_yes"
        elif result == "no":
            pnl     = sell_proceeds.get(sym, 0) - cost
            outcome = "settled_no"
        elif sell_proceeds.get(sym, 0) > 0:
            pnl     = sell_proceeds[sym] - cost
            outcome = "sold"
        else:
            continue

        rows.append({"symbol": sym, "hour": hour,
                     "return_pct": float("nan"), "outcome": outcome,
                     "cost": cost, "sell_proc": sell_proceeds.get(sym, 0),
                     "result": result})

    # Second pass: for YES settlements compute payout from qty
    # Re-fetch qty from fills (we need net held qty)
    buy_qty:  dict[str, float] = defaultdict(float)
    sell_qty: dict[str, float] = defaultdict(float)
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = get("/trade-api/v2/portfolio/fills", params)
        for f in data.get("fills", []):
            t   = f["ticker"]
            qty = float(f["count_fp"])
            if f["action"] == "buy":
                buy_qty[t]  += qty
            else:
                sell_qty[t] += qty
        cursor = data.get("cursor")
        if not cursor or not data.get("fills"):
            break

    for r in rows:
        sym  = r["symbol"]
        cost = r["cost"]
        if r["result"] == "yes":
            net_qty = buy_qty[sym] - sell_qty.get(sym, 0)
            payout  = net_qty * 1.0 + r["sell_proc"]
            pnl     = payout - cost
        elif r["result"] == "no":
            pnl = r["sell_proc"] - cost
        elif r["outcome"] == "sold":
            pnl = r["sell_proc"] - cost
        else:
            continue
        r["return_pct"] = pnl / cost * 100 if cost else float("nan")
        r["pnl"]        = pnl

    return [r for r in rows if r.get("return_pct") == r.get("return_pct")]  # drop nan


def plot_combined(datasets: list[tuple[str, list[dict]]], out_path: Path) -> None:
    """Side-by-side scatter + grouped bar chart comparing multiple datasets."""
    colour_map = {"settled_yes": "green", "settled_no": "red", "sold": "steelblue"}
    ds_colours = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]  # per-dataset
    n = len(datasets)

    fig, axes = plt.subplots(2, n, figsize=(8 * n, 10),
                             gridspec_kw={"height_ratios": [2, 1]})
    if n == 1:
        axes = [[axes[0]], [axes[1]]]

    all_hours = sorted({r["hour"] for _, rows in datasets for r in rows})
    bar_w = 0.8 / n

    for col, (label, rows) in enumerate(datasets):
        hours   = np.array([r["hour"] for r in rows])
        returns = np.array([r["return_pct"] for r in rows])
        ax1, ax2 = axes[0][col], axes[1][col]

        for outcome, colour in colour_map.items():
            mask = np.array([r["outcome"] == outcome for r in rows])
            if mask.any():
                ax1.scatter(hours[mask], returns[mask], c=colour, alpha=0.5,
                            s=30, label=outcome.replace("_", " "))
        ax1.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax1.set_title(label)
        ax1.set_ylabel("Return %")
        ax1.set_xticks(range(24))
        ax1.legend(fontsize=7)

        hour_returns: dict[int, list] = defaultdict(list)
        for r in rows:
            hour_returns[r["hour"]].append(r["return_pct"])

        means  = [np.mean(hour_returns.get(h, [0])) for h in all_hours]
        counts = [len(hour_returns.get(h, [])) for h in all_hours]
        bcolours = ["green" if m >= 0 else "red" for m in means]
        bars = ax2.bar(all_hours, means, color=bcolours, alpha=0.7)
        for bar, cnt in zip(bars, counts):
            if cnt:
                ax2.text(bar.get_x() + bar.get_width() / 2,
                         bar.get_height() + 3,
                         str(cnt), ha="center", va="bottom", fontsize=7)
        ax2.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax2.set_xlabel("Hour of purchase (UTC)")
        ax2.set_ylabel("Mean return %")
        ax2.set_xticks(range(24))

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def main() -> None:
    args     = sys.argv[1:]
    use_api  = "--api" in args
    compare  = "--compare" in args
    out_dir  = None
    if "--out" in args:
        out_dir = Path(args[args.index("--out") + 1])
    positional = [a for a in args if not a.startswith("--")]

    if compare:
        # Merge paper CSV + live API into one dataset and plot together
        run_dir = Path(positional[0]) if positional else latest_run()
        print(f"Loading paper: {run_dir.name}")
        paper   = load(run_dir)
        print("Fetching live from API...")
        live    = load_from_api()
        rows    = paper + live
        label   = f"paper + live ({len(paper)} + {len(live)} positions)"
        out_dir = out_dir or Path("output")
        out_dir.mkdir(parents=True, exist_ok=True)
        _plot(rows, label, out_dir / "return_by_hour_combined.png")
        return

    if use_api:
        print("Fetching from Kalshi API...")
        rows    = load_from_api()
        label   = "live (API)"
        out_dir = out_dir or Path("output/live")
        out_dir.mkdir(parents=True, exist_ok=True)
    elif positional:
        run_dir = Path(positional[0])
        print(f"Run: {run_dir.name}")
        rows    = load(run_dir)
        label   = run_dir.name
        out_dir = run_dir
    else:
        run_dir = latest_run()
        print(f"Run: {run_dir.name}")
        rows    = load(run_dir)
        label   = run_dir.name
        out_dir = run_dir
    if not rows:
        print("No closed positions found.")
        return

    _plot(rows, label, out_dir / "return_by_hour.png")


def _plot(rows: list[dict], label: str, out_path: Path) -> None:
    hours   = np.array([r["hour"] for r in rows])
    returns = np.array([r["return_pct"] for r in rows])

    colour_map = {
        "settled_yes": "green",
        "settled_no":  "red",
        "sold":        "steelblue",
    }

    hour_returns: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        hour_returns[r["hour"]].append(r["return_pct"])

    all_hours = sorted(hour_returns)
    mean_ret  = [np.mean(hour_returns[h]) for h in all_hours]
    counts    = [len(hour_returns[h]) for h in all_hours]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10),
                                    gridspec_kw={"height_ratios": [2, 1]})

    for outcome, colour in colour_map.items():
        mask = np.array([r["outcome"] == outcome for r in rows])
        if mask.any():
            ax1.scatter(hours[mask], returns[mask], c=colour, alpha=0.5, s=30,
                        label=outcome.replace("_", " "))
    ax1.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax1.set_ylabel("Return %")
    ax1.set_title(f"Return % by hour of purchase — {label}")
    ax1.legend(fontsize=8)
    ax1.set_xticks(range(24))

    bar_colours = ["green" if m >= 0 else "red" for m in mean_ret]
    bars = ax2.bar(all_hours, mean_ret, color=bar_colours, alpha=0.7)
    for bar, n in zip(bars, counts):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 5,
                 str(n), ha="center", va="bottom", fontsize=7)
    ax2.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax2.set_xlabel("Hour of purchase (UTC)")
    ax2.set_ylabel("Mean return %")
    ax2.set_xticks(range(24))

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")
    print(f"Positions plotted: {len(rows)}")
    print(f"\nMean return by hour:")
    for h, m, n in zip(all_hours, mean_ret, counts):
        bar_str = "█" * min(int(abs(m) / 10), 30)
        sign    = "+" if m >= 0 else "-"
        print(f"  {h:02d}:xx  {m:+7.1f}%  n={n:3d}  {sign}{bar_str}")


if __name__ == "__main__":
    main()
