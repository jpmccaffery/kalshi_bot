"""
Backfill market_results parquet from the Kalshi API for finalized markets
that are missing from our local data.

Queries each series for all finalized markets, skips any already in the
parquet, and writes the rest.

Usage (inside Docker):
    python scripts/backfill_market_results.py
    python scripts/backfill_market_results.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

import pandas as pd
import requests

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"

KALSHI_BASE = "https://api.elections.kalshi.com"

_EXPIRY_RE = re.compile(r"(\d{2}[A-Z]{3}\d{2})")
_MONTH = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_expiry(ticker: str):
    m = _EXPIRY_RE.search(ticker)
    if not m:
        return None
    s = m.group(1)
    try:
        return dt.date(2000 + int(s[:2]), _MONTH[s[2:5]], int(s[5:]))
    except (KeyError, ValueError):
        return None


def _auth_headers(key_id: str, private_key, method: str, path: str) -> dict:
    from kalshi_bot.auth import auth_headers
    return auth_headers(private_key, key_id, method, path)


def _get(key_id: str, private_key, path: str, params: dict = {}) -> dict:
    from urllib.parse import urlencode
    full_path = path + ("?" + urlencode(params) if params else "")
    headers = _auth_headers(key_id, private_key, "GET", full_path)
    resp = requests.get(KALSHI_BASE + full_path, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def load_existing_tickers() -> set[str]:
    """Load all tickers already in our market_results parquet."""
    path = DATA_DIR / "market" / "market_results"
    if not path.exists():
        return set()
    import pyarrow.dataset as ds
    try:
        d = ds.dataset(path, format="parquet")
        df = d.to_table(columns=["ticker"]).to_pandas()
        return set(df["ticker"].unique())
    except Exception:
        return set()


def fetch_finalized(key_id: str, private_key, series: str) -> list[dict]:
    """Fetch all finalized markets for a series, paginating through results."""
    import time
    rows = []
    cursor = None
    now = dt.datetime.now(tz=dt.timezone.utc)

    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor

        path = "/trade-api/v2/markets"
        try:
            resp = _get(key_id, private_key, path, params)
        except Exception as exc:
            print(f"  ERROR fetching {series}: {exc}")
            break
        time.sleep(0.3)  # stay well under rate limit

        markets = resp.get("markets", [])
        raw_hash = hashlib.sha256(json.dumps(resp, sort_keys=True).encode()).hexdigest()

        for market in markets:
            ticker = market.get("ticker", "")
            if not ticker:
                continue
            expiry_date = _parse_expiry(ticker)

            close_time_raw = market.get("close_time") or market.get("expiration_time")
            close_time = None
            if close_time_raw:
                try:
                    close_time = dt.datetime.fromisoformat(
                        str(close_time_raw).replace("Z", "+00:00")
                    )
                    if close_time.tzinfo is None:
                        close_time = close_time.replace(tzinfo=dt.timezone.utc)
                except (ValueError, TypeError):
                    pass

            rows.append({
                "poll_time":        now,
                "source":           "KALSHI_MARKETS",
                "ticker":           ticker,
                "series":           series,
                "expiry_date":      expiry_date,
                "result":           market.get("result", ""),
                "close_time":       close_time,
                "raw_payload_hash": raw_hash,
                "schema_version":   1,
            })

        cursor = resp.get("cursor")
        if not cursor or not markets:
            break

    return rows


def write_rows(rows: list[dict]) -> None:
    """Write rows to the market_results parquet, partitioned by poll_date."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = pd.DataFrame(rows)
    df["poll_time"]   = pd.to_datetime(df["poll_time"], utc=True)
    df["expiry_date"] = pd.to_datetime(df["expiry_date"])
    df["close_time"]  = pd.to_datetime(df["close_time"], utc=True)

    # Group by expiry date to write into sensible partitions
    for expiry_date, grp in df.groupby(df["expiry_date"].dt.date):
        out_dir = (DATA_DIR / "market" / "market_results"
                   / "source=KALSHI_MARKETS"
                   / f"poll_date={expiry_date.strftime('%Y-%m-%d')}")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"backfill-{ts}.parquet"

        write_df = grp.drop(columns=["expiry_date"])  # partitioned in path
        table = pa.Table.from_pandas(write_df, preserve_index=False)
        pq.write_table(table, out_path)
        print(f"  Wrote {len(grp)} rows → {out_path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill market_results from Kalshi API")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and print counts but don't write")
    args = parser.parse_args()

    from kalshi_bot.config import load_env
    from kalshi_bot.auth import load_private_key
    load_env()

    key_id   = os.environ["KALSHI_API_KEY_ID"]
    key_path = os.environ["KALSHI_API_PRIVATE_KEY_PATH"]
    private_key = load_private_key(key_path)

    series_raw = os.environ.get("KALSHI_SERIES", "").strip()
    if not series_raw:
        print("ERROR: KALSHI_SERIES not set")
        return
    series_list = [s.strip() for s in series_raw.split(",") if s.strip()]

    existing = load_existing_tickers()
    print(f"Existing tickers in parquet: {len(existing)}")

    all_new_rows: list[dict] = []

    for series in series_list:
        print(f"Fetching {series}...")
        rows = fetch_finalized(key_id, private_key, series)
        new  = [r for r in rows if r["ticker"] not in existing]
        print(f"  {len(rows)} finalized, {len(new)} new")
        all_new_rows.extend(new)

    print(f"\nTotal new rows: {len(all_new_rows)}")

    if not all_new_rows:
        print("Nothing to write.")
        return

    if args.dry_run:
        dates = sorted({r["expiry_date"] for r in all_new_rows if r["expiry_date"]})
        print(f"Dry run — would write expiry dates: {dates}")
        return

    write_rows(all_new_rows)
    print("Done.")


if __name__ == "__main__":
    main()
