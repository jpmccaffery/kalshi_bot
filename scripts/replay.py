"""
Replay historical signals with different buy-selection strategies.

Simulates what would have happened if we had used a different strategy
to pick which signals to buy each tick, given the same signals the
recommender actually emitted.

Entry price: meta_yes_ask from signals.csv (the ask at signal time).
Exit price:  1.0 for YES settlement, 0.0 for NO settlement.

Settlement data is taken from settlements.csv (contracts the bot held)
supplemented by querying the Kalshi API for contracts the bot never held.
Results are cached in output/market_results_cache.csv so subsequent runs
don't re-hit the API.

Strategies compared
-------------------
  actual     — orders the bot actually placed (entry = meta_yes_ask for fairness)
  first      — take first N in emission order  (current bot behavior)
  top_edge   — sort by edge descending, take top N
  low_edge   — sort by edge ascending (lowest edge first), take bottom N

Usage
-----
    python scripts/replay.py                        # all strategies, N=6
    python scripts/replay.py --n 3 6 10 15 all     # sweep N values
    python scripts/replay.py --no-fetch             # skip API calls, use cache only
"""

from __future__ import annotations

import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT   = Path(__file__).parent.parent
OUTPUT_DIR  = REPO_ROOT / "output"
CACHE_PATH  = OUTPUT_DIR / "market_results_cache.csv"
KALSHI_BASE = "https://api.elections.kalshi.com"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _run_dirs_with_settlements() -> list[Path]:
    return [d for d in sorted(OUTPUT_DIR.glob("run_*"))
            if (d / "settlements.csv").exists() and (d / "signals.csv").exists()]


def load_settlements(run_dirs: list[Path]) -> dict[str, dict]:
    """{ symbol -> {result, ts} } from settlements.csv files."""
    out: dict[str, dict] = {}
    for d in run_dirs:
        path = d / "settlements.csv"
        with path.open() as f:
            for row in csv.DictReader(f):
                out[row["symbol"]] = {"result": row["result"], "ts": row["ts"]}
    return out


def load_signals_by_tick(run_dirs: list[Path]) -> list[tuple[str, list[dict]]]:
    """Sorted list of (tick_timestamp_str, [signal_rows])."""
    by_tick: dict[str, list[dict]] = defaultdict(list)
    for d in run_dirs:
        path = d / "signals.csv"
        with path.open() as f:
            for row in csv.DictReader(f):
                by_tick[row["date"]].append(row)
    return sorted(by_tick.items())


def load_actual_symbols(run_dirs: list[Path]) -> set[str]:
    """Set of symbols the bot actually bought (filled_qty > 0)."""
    bought: set[str] = set()
    for d in run_dirs:
        path = d / "orders.csv"
        if not path.exists():
            continue
        with path.open() as f:
            for row in csv.DictReader(f):
                if row.get("side") == "buy" and float(row.get("filled_qty") or 0) > 0:
                    bought.add(row["symbol"])
    return bought


# ---------------------------------------------------------------------------
# Market result cache (supplements settlements.csv for untraded contracts)
# ---------------------------------------------------------------------------

def load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    with CACHE_PATH.open() as f:
        return {row["ticker"]: row for row in csv.DictReader(f)}


def save_cache(cache: dict[str, dict]) -> None:
    with CACHE_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "result", "ts"])
        w.writeheader()
        w.writerows(cache.values())


def _kalshi_close_time_to_ts(close_time: str) -> str:
    """Convert '2026-05-09T08:00:00Z' → '2026-05-09 08:00' for tick comparison."""
    return close_time.replace("T", " ")[:16]


def fetch_and_cache(all_tickers: set[str], no_fetch: bool = False) -> dict[str, dict]:
    """
    For every ticker not in the cache, query Kalshi and cache the result.
    Returns merged cache: {ticker -> {ticker, result, ts}}.
    result is 'yes', 'no', 'void', or '' (not yet settled).
    """
    cache = load_cache()
    missing = sorted(all_tickers - set(cache))

    if not missing:
        return cache

    if no_fetch:
        print(f"  {len(missing)} tickers missing from cache; skipping fetch (--no-fetch)")
        return cache

    print(f"  Fetching {len(missing)} market results from Kalshi API...")

    from kalshi_bot.auth import auth_headers, load_private_key
    from dotenv import load_dotenv
    import os
    import requests

    load_dotenv()
    key    = load_private_key(os.environ["KALSHI_API_PRIVATE_KEY_PATH"])
    key_id = os.environ["KALSHI_API_KEY_ID"]

    fetched = 0
    for i, ticker in enumerate(missing):
        path    = f"/trade-api/v2/markets/{ticker}"
        headers = auth_headers(key, key_id, "GET", path)
        try:
            r = requests.get(KALSHI_BASE + path, headers=headers, timeout=10)
        except Exception as e:
            print(f"\n  Warning: request failed for {ticker}: {e}")
            continue

        if r.status_code != 200:
            continue

        market = r.json().get("market", {})
        result = market.get("result", "")
        close  = market.get("close_time", "")
        if not result or not close:
            continue  # not yet settled — don't cache

        cache[ticker] = {
            "ticker": ticker,
            "result": result,
            "ts":     _kalshi_close_time_to_ts(close),
        }
        fetched += 1

        if (i + 1) % 20 == 0 or (i + 1) == len(missing):
            print(f"  {i + 1}/{len(missing)} fetched ({fetched} settled)", end="\r")
        time.sleep(0.05)

    print(f"\n  Done. {fetched} new results cached.")
    save_cache(cache)
    return cache


def build_settlements(
    run_dirs: list[Path],
    no_fetch: bool = False,
) -> dict[str, dict]:
    """
    Merge settlements.csv (authoritative, has exact tick ts) with the
    market-results cache (approximate ts = close_time).
    settlements.csv takes priority for any ticker it covers.
    """
    from_csv   = load_settlements(run_dirs)
    all_tickers = _all_signal_tickers(run_dirs)
    cache       = fetch_and_cache(all_tickers, no_fetch=no_fetch)

    merged = dict(cache)   # start with cache (approximate ts)
    merged.update(from_csv)  # csv wins (exact ts)
    return merged


def _all_signal_tickers(run_dirs: list[Path]) -> set[str]:
    tickers: set[str] = set()
    for d in run_dirs:
        path = d / "signals.csv"
        if not path.exists():
            continue
        with path.open() as f:
            for row in csv.DictReader(f):
                tickers.add(row["symbol"])
    return tickers


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def simulate(
    signals_by_tick: list[tuple[str, list[dict]]],
    settlements: dict[str, dict],
    n: int | None,
    rank_fn,
    allowed_symbols: set[str] | None = None,
) -> list[dict]:
    """
    Tick-by-tick simulation.

    n              : max buys per tick (None = unlimited)
    rank_fn        : callable(signals) -> ordered signals
    allowed_symbols: if set, only buy symbols in this set (used for 'actual' baseline)
    """
    held:         set[str]         = set()
    entry_prices: dict[str, float] = {}
    results:      list[dict]       = []

    for tick_ts, tick_signals in signals_by_tick:
        # Close positions that have settled at or before this tick
        to_close = [s for s in list(held)
                    if s in settlements and settlements[s]["ts"] <= tick_ts]
        for sym in to_close:
            held.discard(sym)
            entry = entry_prices.pop(sym)
            res   = settlements[sym]["result"]
            exit_price = 1.0 if res == "yes" else 0.0
            results.append({
                "symbol":     sym,
                "entry":      entry,
                "result":     res,
                "return_pct": (exit_price - entry) / entry * 100 if entry else None,
            })

        # Eligible signals: not already held, optional symbol filter
        eligible = [s for s in tick_signals if s["symbol"] not in held]
        if allowed_symbols is not None:
            eligible = [s for s in eligible if s["symbol"] in allowed_symbols]

        ranked = rank_fn(eligible)
        if n is not None:
            ranked = ranked[:n]

        for sig in ranked:
            sym   = sig["symbol"]
            entry = float(sig.get("meta_yes_ask") or 0)
            if entry <= 0:
                continue
            held.add(sym)
            entry_prices[sym] = entry

    # Positions still open at end of data
    for sym in held:
        results.append({
            "symbol":     sym,
            "entry":      entry_prices[sym],
            "result":     "open",
            "return_pct": None,
        })

    return results


# ---------------------------------------------------------------------------
# Stats + display
# ---------------------------------------------------------------------------

def compute_stats(results: list[dict]) -> dict:
    closed  = [r for r in results if r["result"] != "open"]
    yes     = [r for r in closed  if r["result"] == "yes"]
    returns = [r["return_pct"] for r in closed if r["return_pct"] is not None]
    # cap_ret: (total_earned - total_deployed) / total_deployed
    # With fixed $1/trade: total_deployed = n, dollar_pnl_i = return_pct_i / 100
    # so cap_ret = mean(return_pct) — same as mean_ret for uniform trade sizes.
    # With real varying sizes these would diverge; tracked separately for consistency.
    n = len(returns)
    cap_ret = sum(returns) / n if n else float("nan")
    return {
        "total":      len(results),
        "settled":    len(closed),
        "open":       len(results) - len(closed),
        "win_pct":    len(yes) / len(closed) * 100 if closed else float("nan"),
        "cap_ret":    cap_ret,
        "mean_ret":   sum(returns) / n if n else float("nan"),
        "median_ret": sorted(returns)[n // 2] if n else float("nan"),
        "sum_ret":    sum(returns),
    }


def fmt_pct(v: float) -> str:
    return "      ?" if v != v else f"{v:+7.1f}%"


def _expiry(sym: str) -> str:
    import re
    m = re.search(r"\d{2}[A-Z]{3}\d{2}", sym)
    return m.group() if m else "?"


def print_by_day(label: str, results: list[dict]) -> None:
    from collections import defaultdict
    by_exp: dict[str, list] = defaultdict(list)
    for r in results:
        by_exp[_expiry(r["symbol"])].append(r)

    col = "  {:10s} {:>6s} {:>7s} {:>5s} {:>7s} {:>9s}"
    print(f"\n{label}")
    print(col.format("expiry", "pos", "settled", "open", "win%", "mean_ret"))
    print("  " + "-" * 50)
    for exp in sorted(by_exp):
        s = compute_stats(by_exp[exp])
        print(col.format(
            exp, str(s["total"]), str(s["settled"]), str(s["open"]),
            fmt_pct(s["win_pct"]), fmt_pct(s["mean_ret"]),
        ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args     = sys.argv[1:]
    no_fetch = "--no-fetch" in args
    by_day   = "--by-day"   in args

    n_values: list[int | None] = [3, 6, 10, 15, None]
    if "--n" in args:
        idx = args.index("--n")
        n_values = []
        for a in args[idx + 1:]:
            if a.startswith("--"):
                break
            n_values.append(None if a == "all" else int(a))

    run_dirs = _run_dirs_with_settlements()
    if not run_dirs:
        print("No run directories with settlements.csv found.")
        return

    print(f"Runs: {', '.join(d.name for d in run_dirs)}")

    settlements     = build_settlements(run_dirs, no_fetch=no_fetch)
    signals_by_tick = load_signals_by_tick(run_dirs)
    actual_symbols  = load_actual_symbols(run_dirs)

    n_settled = sum(1 for v in settlements.values() if v["result"] in ("yes", "no", "void"))
    print(f"Ticks: {len(signals_by_tick)}  "
          f"Signals: {sum(len(v) for _, v in signals_by_tick)}  "
          f"Settlements known: {n_settled}  "
          f"Actually traded: {len(actual_symbols)}\n")

    rank_fns = {
        "first":    lambda sigs: list(sigs),
        "top_edge": lambda sigs: sorted(sigs, key=lambda s: float(s.get("edge") or 0), reverse=True),
        "low_edge": lambda sigs: sorted(sigs, key=lambda s: float(s.get("edge") or 0)),
    }

    col = "{:12s} {:>5s} {:>6s} {:>7s} {:>5s} {:>7s} {:>9s} {:>9s} {:>9s}"
    print(col.format("strategy", "N", "pos", "settled", "open", "win%", "cap_ret", "avg_ret", "med_ret"))
    print("-" * 82)

    def print_row(name: str, n_label: str, results: list[dict]) -> None:
        s = compute_stats(results)
        print(col.format(
            name, n_label,
            str(s["total"]), str(s["settled"]), str(s["open"]),
            fmt_pct(s["win_pct"]),
            fmt_pct(s["cap_ret"]),
            fmt_pct(s["mean_ret"]),
            fmt_pct(s["median_ret"]),
        ))

    actual_results = simulate(signals_by_tick, settlements, n=None,
                              rank_fn=lambda s: s, allowed_symbols=actual_symbols)
    print_row("actual", "~6", actual_results)
    if by_day:
        print_by_day("actual ~6", actual_results)
    print()

    for strategy, rank_fn in rank_fns.items():
        for n in n_values:
            results = simulate(signals_by_tick, settlements, n, rank_fn)
            n_label = "all" if n is None else str(n)
            print_row(strategy, n_label, results)
            if by_day:
                print_by_day(f"{strategy} N={n_label}", results)
        print()


if __name__ == "__main__":
    main()
