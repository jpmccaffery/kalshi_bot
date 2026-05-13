"""
Scatter plot of edge (model_prob - entry_price) vs pnl per transaction.

Usage:
    python scripts/plot_edge_vs_pnl.py                        # all settled/sold
    python scripts/plot_edge_vs_pnl.py output/transaction_summary_26MAY09.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.lines as mlines
import numpy as np
import pandas as pd

REPO_ROOT  = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "output"


def main() -> None:
    if len(sys.argv) > 1:
        csv_path = Path(sys.argv[1])
    else:
        csv_path = OUTPUT_DIR / "transaction_summary.csv"

    df = pd.read_csv(csv_path)

    # Only rows with both model_prob and pnl
    df = df[df["model_prob"].notna() & df["pnl"].notna() & (df["model_prob"] != "")]
    df["model_prob"]  = pd.to_numeric(df["model_prob"],  errors="coerce")
    df["buy_price"]   = pd.to_numeric(df["buy_price"],   errors="coerce")
    df["pnl"]         = pd.to_numeric(df["pnl"],         errors="coerce")
    df["edge"]        = pd.to_numeric(df["edge"],         errors="coerce")
    df = df.dropna(subset=["model_prob", "buy_price", "pnl"])

    df["gross_edge"] = df["model_prob"] - df["buy_price"]

    colour_map = {
        "settled_yes": "green",
        "settled_no":  "red",
        "sold":        "steelblue",
    }
    df["colour"] = df["exit_type"].map(colour_map).fillna("grey")

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    for ax, xcol, xlabel in [
        (axes[0], "gross_edge", "Gross edge  (model_prob − entry_price)"),
        (axes[1], "edge",       "Net edge  (model_prob − entry_price − fee)"),
    ]:
        for exit_type, colour in colour_map.items():
            mask = df["exit_type"] == exit_type
            sub  = df[mask]
            if sub.empty:
                continue
            ax.scatter(sub[xcol], sub["pnl"], c=colour, alpha=0.6, s=40,
                       label=exit_type.replace("_", " "))

        # Zero lines
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax.axvline(0, color="black", linewidth=0.8, linestyle="--")

        # Trend line on closed positions
        closed = df[df["exit_type"].isin(colour_map)]
        if len(closed) > 1:
            x = closed[xcol].values
            y = closed["pnl"].values
            m, b = np.polyfit(x, y, 1)
            xline = np.linspace(x.min(), x.max(), 100)
            ax.plot(xline, m * xline + b, color="black", linewidth=1.2,
                    linestyle="-", alpha=0.5, label=f"trend (slope={m:.0f})")

        ax.set_xlabel(xlabel)
        ax.set_ylabel("PnL ($)")
        ax.set_title(xlabel)
        ax.legend(fontsize=8)

    n_settled = (df["exit_type"].isin(["settled_yes", "settled_no"])).sum()
    n_sold    = (df["exit_type"] == "sold").sum()
    fig.suptitle(
        f"{csv_path.name}  —  {n_settled} settled, {n_sold} sold early",
        fontsize=12, fontweight="bold"
    )
    fig.tight_layout()

    out_path = csv_path.parent / (csv_path.stem + "_edge_vs_pnl.png")
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
