"""
Join orders.csv with signals.csv to produce orders_augmented.csv with model data.

Usage:
    python scripts/augment_orders.py output/run_20260505_195337
    python scripts/augment_orders.py          # latest run in output/
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def latest_run(output_dir: Path) -> Path:
    runs = sorted(output_dir.glob("run_*"), reverse=True)
    if not runs:
        raise FileNotFoundError(f"No run directories in {output_dir}")
    return runs[0]


def main() -> None:
    repo_root  = Path(__file__).parent.parent
    output_dir = repo_root / "output"

    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_run(output_dir)
    print(f"Run: {run_dir}")

    orders  = pd.read_csv(run_dir / "orders.csv")
    signals = pd.read_csv(run_dir / "signals.csv")

    # Rename signal meta columns to drop the meta_ prefix
    signals = signals.rename(columns={
        c: c.replace("meta_", "") for c in signals.columns if c.startswith("meta_")
    })

    # Keep only the signal columns useful for augmentation
    sig_cols = ["date", "symbol", "edge", "conviction",
                "model_prob", "yes_ask", "fee", "days_out",
                "city", "kind", "expiry", "contract"]
    sig_cols = [c for c in sig_cols if c in signals.columns]
    signals  = signals[sig_cols].drop_duplicates(subset=["date", "symbol"])

    # Join on date + symbol (buys only — sells and settlements have no signal row)
    augmented = orders.merge(signals, on=["date", "symbol"], how="left")

    out_path = run_dir / "orders_augmented.csv"
    augmented.to_csv(out_path, index=False)
    print(f"Wrote {len(augmented)} rows → {out_path}")
    print(augmented[augmented["side"] == "buy"][
        ["date", "symbol", "fill_price", "filled_qty", "model_prob", "edge", "days_out"]
    ].to_string(index=False))


if __name__ == "__main__":
    main()
