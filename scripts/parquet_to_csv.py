"""
Convert a parquet file or partitioned parquet dataset to CSV.

Usage:
    python scripts/parquet_to_csv.py <path>              # prints to stdout
    python scripts/parquet_to_csv.py <path> <out.csv>    # writes to file
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow.dataset as ds


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: parquet_to_csv.py <path> [out.csv]", file=sys.stderr)
        sys.exit(1)

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) >= 3 else None

    dataset = ds.dataset(src, format="parquet")
    df = dataset.to_table().to_pandas()

    if dst:
        df.to_csv(dst, index=False)
        print(f"Wrote {len(df):,} rows to {dst}")
    else:
        print(df.to_csv(index=False), end="")


if __name__ == "__main__":
    main()
