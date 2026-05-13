"""
Plot the bid-price trend for every bought contract, normalised to entry price.

Usage:
    python scripts/plot_position_trends.py output/run_20260429_224400
    python scripts/plot_position_trends.py          # latest run in output/
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import pandas as pd
import numpy as np


def latest_run(output_dir: Path) -> Path:
    runs = sorted(output_dir.glob("run_*"), reverse=True)
    if not runs:
        raise FileNotFoundError(f"No run directories in {output_dir}")
    return runs[0]


def main() -> None:
    repo_root  = Path(__file__).parent.parent
    output_dir = repo_root / "output"

    if len(sys.argv) > 1:
        run_dir = Path(sys.argv[1])
    else:
        run_dir = latest_run(output_dir)

    print(f"Reading from: {run_dir}")

    pos_path    = run_dir / "positions_eval.csv"
    orders_path = run_dir / "orders.csv"

    if not pos_path.exists():
        print(f"No positions_eval.csv in {run_dir}")
        sys.exit(1)

    pos = pd.read_csv(pos_path, parse_dates=["ts"])
    pos = pos[pos["yes_bid"].notna() & pos["entry_price"].notna()]
    pos["pnl_pct"] = (pos["yes_bid"] - pos["entry_price"]) / pos["entry_price"] * 100

    # Determine final outcome per ticker from orders (sell reason) or leave unknown
    outcomes: dict[str, str] = {}
    if orders_path.exists():
        orders = pd.read_csv(orders_path)
        sells  = orders[orders["side"] == "sell"]
        for _, row in sells.iterrows():
            ticker = row["symbol"]
            reason = str(row.get("reason", ""))
            price  = float(row.get("fill_price") or 0)
            if "settle" in reason.lower():
                outcomes[ticker] = "settled_yes" if price >= 0.99 else "settled_no"
            elif reason == "model_overpriced":
                outcomes[ticker] = "sold_profit" if price > float(
                    orders[orders["symbol"] == ticker]["limit_price"].iloc[0]
                ) else "sold_loss"
            else:
                outcomes[ticker] = "sold"

    tickers = pos["ticker"].unique()
    n       = len(tickers)
    print(f"Found {n} tickers")

    # Colour palette: green=sold_profit/settled_yes, red=settled_no/loss, grey=unknown/open
    def colour(ticker: str) -> str:
        o = outcomes.get(ticker, "open")
        if o in ("settled_yes", "sold_profit"):
            return "green"
        if o in ("settled_no", "sold_loss"):
            return "red"
        if o == "sold":
            return "steelblue"
        return "grey"

    fig, ax = plt.subplots(figsize=(14, 7))

    for ticker in tickers:
        df = pos[pos["ticker"] == ticker].sort_values("ts").copy()
        df["tick"] = range(len(df))
        c = colour(ticker)
        ax.plot(df["tick"], df["pnl_pct"], color=c, alpha=0.5, linewidth=1.2)
        # Label the last point
        last = df.iloc[-1]
        ax.annotate(
            ticker.split("-")[-1],     # just the contract part, e.g. "T60"
            (last["tick"], last["pnl_pct"]),
            fontsize=6, alpha=0.7, color=c,
        )

    ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Ticks held")
    ax.set_ylabel("PnL % vs entry")
    ax.set_title(f"Position price trends — {run_dir.name}")

    # Legend
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], color="green",    label="Settled YES / sold profit"),
        Line2D([0], [0], color="red",      label="Settled NO / sold loss"),
        Line2D([0], [0], color="steelblue",label="Sold (reason unknown)"),
        Line2D([0], [0], color="grey",     label="Still open"),
    ]
    ax.legend(handles=legend_elements, fontsize=8)

    out_path = run_dir / "position_trends.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")

    # --- Histograms: pnl% at various tick horizons ---
    horizons = [1, 3, 5, 10, 15, 20]
    tick_pnls: dict[int, list[float]] = {h: [] for h in horizons}
    for ticker in tickers:
        df = pos[pos["ticker"] == ticker].sort_values("ts").reset_index(drop=True)
        for h in horizons:
            if len(df) > h:
                tick_pnls[h].append(df.loc[h, "pnl_pct"])

    ncols = 3
    nrows = int(np.ceil(len(horizons) / ncols))
    fig2, axes = plt.subplots(nrows, ncols, figsize=(14, 5 * nrows), sharey=False)
    axes_flat = axes.flatten() if nrows > 1 else list(axes)
    for ax2, h in zip(axes_flat, horizons):
        data = tick_pnls[h]
        if not data:
            ax2.set_title(f"After {h} tick(s) — no data")
            ax2.set_visible(True)
            continue
        mean_val = float(pd.Series(data).mean())
        ax2.hist(data, bins=20, color="steelblue", edgecolor="white", alpha=0.85)
        ax2.axvline(0, color="black", linewidth=0.9, linestyle="--")
        ax2.axvline(mean_val, color="red", linewidth=1.2, linestyle="-",
                    label=f"mean={mean_val:.1f}%")
        ax2.set_xlabel("PnL % vs entry")
        ax2.set_ylabel("Count")
        ax2.set_title(f"After {h} tick(s)  (n={len(data)})")
        ax2.legend(fontsize=8)
    for ax2 in axes_flat[len(horizons):]:
        ax2.set_visible(False)

    fig2.suptitle(f"PnL distribution by ticks held — {run_dir.name}", fontsize=11)
    fig2.tight_layout()
    hist_path = run_dir / "position_histograms.png"
    fig2.savefig(hist_path, dpi=150)
    print(f"Saved: {hist_path}")

    # --- Mean PnL vs ticks held (summary line chart) ---
    all_horizons = list(range(1, 25))
    mean_pnl_by_tick: dict[int, float] = {}
    n_by_tick: dict[int, int] = {}
    for h in all_horizons:
        vals = []
        for ticker in tickers:
            df = pos[pos["ticker"] == ticker].sort_values("ts").reset_index(drop=True)
            if len(df) > h:
                vals.append(df.loc[h, "pnl_pct"])
        if vals:
            mean_pnl_by_tick[h] = float(pd.Series(vals).mean())
            n_by_tick[h] = len(vals)

    if mean_pnl_by_tick:
        fig3, ax3 = plt.subplots(figsize=(10, 5))
        xs = sorted(mean_pnl_by_tick)
        ys = [mean_pnl_by_tick[x] for x in xs]
        ns = [n_by_tick[x] for x in xs]
        ax3.plot(xs, ys, color="steelblue", linewidth=2, marker="o", markersize=4)
        ax3.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax3.fill_between(xs, ys, 0, where=[y >= 0 for y in ys],
                         alpha=0.2, color="green")
        ax3.fill_between(xs, ys, 0, where=[y < 0 for y in ys],
                         alpha=0.2, color="red")
        for x, y, n in zip(xs, ys, ns):
            if x % 5 == 0:
                ax3.annotate(f"n={n}", (x, y), textcoords="offset points",
                             xytext=(0, 6), fontsize=7, ha="center")
        ax3.set_xlabel("Ticks held")
        ax3.set_ylabel("Mean PnL % vs entry")
        ax3.set_title(f"Mean PnL by ticks held — {run_dir.name}")
        fig3.tight_layout()
        mean_path = run_dir / "pnl_by_ticks.png"
        fig3.savefig(mean_path, dpi=150)
        print(f"Saved: {mean_path}")


if __name__ == "__main__":
    main()
