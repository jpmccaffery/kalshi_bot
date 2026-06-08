"""
Analysis plots for the Kalshi bot.

Usage:
    python scripts/plot_analysis.py                          # output/transaction_summary.csv
    python scripts/plot_analysis.py output/live/             # specific dir
    python scripts/plot_analysis.py --csv path/to/file.csv
    python scripts/plot_analysis.py --out output/plots/
    python scripts/plot_analysis.py output/paper_both --start 2026-05-23 --end 2026-06-01
    python scripts/plot_analysis.py output/bt_test --start 2026-05-23 --end 2026-06-01 --out output/bt_test_filtered

Plots produced:
  return_by_hour.png         — return % by UTC hour of purchase
  return_by_hour_by_day.png      — return % by local hour, split by same-day / 1-day-out / 2+-day-out
  return_by_utc_hour_by_day.png  — same but by UTC hour
  return_by_staleness.png    — return % by signal staleness, split by day bucket
  early_sell_analysis.png    — actual vs theoretical hold-to-settlement return for early sells
  return_vs_edge.png         — return % vs edge at purchase
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

from plot_common import REPO_ROOT, load, find_csv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_results_cache() -> dict[str, str]:
    path = REPO_ROOT / "output" / "market_results_cache.csv"
    results = {}
    if path.exists():
        with path.open() as f:
            for r in csv.DictReader(f):
                if r.get("result") in ("yes", "no"):
                    results[r["ticker"]] = r["result"]
    return results


def _load_settlements(csv_path: Path) -> dict[str, str]:
    results = {}
    for settle_path in csv_path.parent.glob("run_*/settlements.csv"):
        with settle_path.open() as f:
            for r in csv.DictReader(f):
                if r.get("result") in ("yes", "no"):
                    results[r["symbol"]] = r["result"]
    return results


def _fetch_results_from_api(tickers: list[str]) -> dict[str, str]:
    try:
        import os, requests as _requests
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env", override=False)
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from kalshi_bot.auth import auth_headers, load_private_key
        key    = load_private_key(os.environ["KALSHI_API_PRIVATE_KEY_PATH"])
        key_id = os.environ["KALSHI_API_KEY_ID"]
        base   = "https://api.elections.kalshi.com"
    except Exception as e:
        print(f"  API auth failed: {e}")
        return {}
    results = {}
    for ticker in tickers:
        try:
            path = f"/trade-api/v2/markets/{ticker}"
            h = auth_headers(key, key_id, "GET", path)
            r = _requests.get(base + path, headers=h, timeout=10)
            result = r.json().get("market", {}).get("result", "")
            if result in ("yes", "no"):
                results[ticker] = result
        except Exception:
            pass
    return results


def _scatter_bar(ax_scatter, ax_bar, xs, returns, colours, all_x, label: str, xlabel: str):
    for colour, lbl in [("green", "profit"), ("red", "loss"), ("steelblue", "sold early")]:
        mask = colours == colour
        if mask.any():
            ax_scatter.scatter(xs[mask], returns[mask], c=colour, alpha=0.5, s=30, label=lbl)
    ax_scatter.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax_scatter.set_ylabel("Return %")
    ax_scatter.set_title(label)
    ax_scatter.legend(fontsize=7)

    bucket: dict[float, list] = defaultdict(list)
    for x, r in zip(xs, returns):
        bucket[x].append(r)
    means   = [np.mean(bucket.get(x, [0])) for x in all_x]
    counts  = [len(bucket.get(x, [])) for x in all_x]
    bcolours = ["green" if m >= 0 else "red" for m in means]
    bars = ax_bar.bar(all_x, means, color=bcolours, alpha=0.7)
    for bar, cnt in zip(bars, counts):
        if cnt:
            ax_bar.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + (3 if bar.get_height() >= 0 else -8),
                        str(cnt), ha="center", va="bottom", fontsize=7)
    ax_bar.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax_bar.set_xlabel(xlabel)
    ax_bar.set_ylabel("Mean return %")


def _day_bucket(d: float) -> str:
    if d < 0.5:   return "same day"
    if d < 1.5:   return "1 day out"
    return "2+ days out"


# ---------------------------------------------------------------------------
# Plot functions
# ---------------------------------------------------------------------------

def plot_by_hour(rows: list[dict], label: str, out_path: Path) -> None:
    hours   = np.array([r["hour"] for r in rows])
    returns = np.array([r["return_pct"] for r in rows])
    colours = np.array(["green" if r["return_pct"] > 0 else "red" if r["return_pct"] < 0 else "steelblue" for r in rows])
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), gridspec_kw={"height_ratios": [2, 1]})
    _scatter_bar(ax1, ax2, hours, returns, colours, list(range(24)),
                 f"Return % by hour of purchase — {label}", "Hour of purchase (UTC)")
    ax1.set_xticks(range(24))
    ax2.set_xticks(range(24))
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def plot_by_hour_and_day(rows: list[dict], label: str, out_path: Path,
                         use_utc: bool = False) -> None:
    groups: dict[str, list] = {"same day": [], "1 day out": [], "2+ days out": []}
    for r in rows:
        groups[_day_bucket(r["local_days_out"])].append(r)

    n_cols = sum(1 for g in groups.values() if g)
    if not n_cols:
        return
    hour_key  = "hour" if use_utc else "local_hour"
    tz_label  = "UTC" if use_utc else "city local time"
    fig, axes = plt.subplots(2, n_cols, figsize=(7 * n_cols, 9),
                              gridspec_kw={"height_ratios": [2, 1]}, squeeze=False)
    fig.suptitle(f"Return % by {'UTC' if use_utc else 'local'} hour of purchase — {label}", fontsize=12)
    col = 0
    for day_label in ("same day", "1 day out", "2+ days out"):
        grp = groups[day_label]
        if not grp:
            continue
        hours   = np.array([r[hour_key] for r in grp])
        returns = np.array([r["return_pct"] for r in grp])
        colours = np.array(["green" if r["return_pct"] > 0 else "red" if r["return_pct"] < 0 else "steelblue" for r in grp])
        _scatter_bar(axes[0][col], axes[1][col], hours, returns, colours, list(range(24)),
                     f"{day_label} (n={len(grp)})", f"Hour of purchase ({tz_label})")
        axes[0][col].set_xticks(range(0, 24, 2))
        axes[1][col].set_xticks(range(0, 24, 2))
        col += 1
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def plot_by_staleness(rows: list[dict], label: str, out_path: Path) -> None:
    groups: dict[str, list] = {"same day": [], "1 day out": [], "2+ days out": []}
    for r in rows:
        groups[_day_bucket(r["local_days_out"])].append(r)

    all_bins = list(range(6))
    n_cols = sum(1 for g in groups.values() if g)
    if not n_cols:
        return
    fig, axes = plt.subplots(2, n_cols, figsize=(6 * n_cols, 9),
                              gridspec_kw={"height_ratios": [2, 1]}, squeeze=False)
    fig.suptitle(f"Return % by signal staleness — {label}", fontsize=12)
    col = 0
    for day_label in ("same day", "1 day out", "2+ days out"):
        grp = groups[day_label]
        if not grp:
            continue
        stal    = np.array([min(int(r["staleness"]), 5) for r in grp])
        returns = np.array([r["return_pct"] for r in grp])
        colours = np.array(["green" if r["return_pct"] > 0 else "red" if r["return_pct"] < 0 else "steelblue" for r in grp])
        _scatter_bar(axes[0][col], axes[1][col], stal, returns, colours, all_bins,
                     day_label, "Hours since last forecast update")
        axes[0][col].set_xticks(all_bins)
        axes[0][col].set_xticklabels([f"{b}-{b+1}h" for b in all_bins], fontsize=8)
        axes[1][col].set_xticks(all_bins)
        axes[1][col].set_xticklabels([f"{b}-{b+1}h" for b in all_bins], fontsize=8)
        col += 1
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def plot_early_sell(rows: list[dict], results: dict[str, str],
                    label: str, out_path: Path) -> None:
    sold = [r for r in rows if r["exit_type"] == "sold"]
    actual, theoretical, missing = [], [], 0
    for r in sold:
        result = results.get(r["symbol"])
        if result is None:
            missing += 1
            continue
        act = r["return_pct"]
        theo_proceeds = r["quantity"] if result == "yes" else 0.0
        theo = (theo_proceeds - r["cost"]) / r["cost"] * 100
        actual.append(act)
        theoretical.append(theo)

    if not actual:
        print(f"Early sell: no sold positions with known results (missing={missing})")
        return

    actual      = np.array(actual)
    theoretical = np.array(theoretical)
    better_sold = actual > theoretical

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    ax1.scatter(actual[better_sold], theoretical[better_sold],
                color="green", alpha=0.6, s=40, label=f"Right to sell ({better_sold.sum()})")
    ax1.scatter(actual[~better_sold], theoretical[~better_sold],
                color="red", alpha=0.6, s=40, label=f"Should have held ({(~better_sold).sum()})")
    lim = max(abs(actual).max(), abs(theoretical).max()) * 1.1
    ax1.plot([-lim, lim], [-lim, lim], "k--", linewidth=0.8, label="break-even (x=y)")
    ax1.axhline(0, color="grey", linewidth=0.5)
    ax1.axvline(0, color="grey", linewidth=0.5)
    ax1.set_xlabel("Actual return % (sold early)")
    ax1.set_ylabel("Theoretical return % (if held to settlement)")
    ax1.set_title(f"Early sell vs hold — {label}\n(above diagonal = should have held)")
    ax1.legend(fontsize=8)

    means = [float(actual.mean()), float(theoretical.mean())]
    bars  = ax2.bar(["Sold early\n(actual)", "Hold to settle\n(theoretical)"],
                    means, color=["green" if m >= 0 else "red" for m in means], alpha=0.7, width=0.4)
    for bar, m in zip(bars, means):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + (5 if m >= 0 else -12),
                 f"{m:+.1f}%", ha="center", fontsize=10, fontweight="bold")
    ax2.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax2.set_ylabel("Mean return %")
    ax2.set_title(f"Mean return: sell vs hold\nn={len(actual)}, {missing} missing results")

    if missing:
        print(f"  Early sell: {missing} sold positions had no known result and were excluded")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def plot_return_vs_edge(rows: list[dict], label: str, out_path: Path) -> None:
    edges   = np.array([r["edge"] for r in rows])
    returns = np.array([r["return_pct"] for r in rows])
    colours = np.array(["green" if r > 0 else "red" for r in returns])

    bucket_size  = 0.05
    bucket_start = np.floor(max(0.0, edges.min()) / bucket_size) * bucket_size
    bucket_edges = np.arange(bucket_start, edges.max() + bucket_size, bucket_size)
    bucket_labels = [f"{b:.2f}-{b+bucket_size:.2f}" for b in bucket_edges[:-1]]

    bucket_returns: dict[int, list] = defaultdict(list)
    for e, r in zip(edges, returns):
        idx = max(0, min(int((e - bucket_edges[0]) / bucket_size), len(bucket_labels) - 1))
        bucket_returns[idx].append(r)

    means  = [np.mean(bucket_returns[i]) if bucket_returns[i] else 0 for i in range(len(bucket_labels))]
    counts = [len(bucket_returns[i]) for i in range(len(bucket_labels))]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9), gridspec_kw={"height_ratios": [2, 1]})
    for colour, lbl in [("green", "profit"), ("red", "loss")]:
        mask = colours == colour
        if mask.any():
            ax1.scatter(edges[mask], returns[mask], c=colour, alpha=0.5, s=30, label=lbl)
    ax1.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax1.set_ylabel("Return %")
    ax1.set_title(f"Return % vs edge at purchase — {label}")
    ax1.legend(fontsize=8)

    x_pos = np.arange(len(bucket_labels))
    bars  = ax2.bar(x_pos, means, color=["green" if m >= 0 else "red" for m in means], alpha=0.7)
    for bar, cnt in zip(bars, counts):
        if cnt:
            ax2.text(bar.get_x() + bar.get_width() / 2,
                     bar.get_height() + (3 if bar.get_height() >= 0 else -10),
                     str(cnt), ha="center", va="bottom", fontsize=7)
    ax2.set_xticks(x_pos)
    ax2.set_xticklabels(bucket_labels, rotation=45, ha="right", fontsize=8)
    ax2.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax2.set_xlabel("Edge at purchase")
    ax2.set_ylabel("Mean return %")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")

    print("\nMean return by edge bucket:")
    for lbl, m, n in zip(bucket_labels, means, counts):
        if n:
            sign = "-" if m < 0 else "+"
            print(f"  {lbl}  {m:+7.1f}%  n={n:3d}  {sign}{'█' * min(int(abs(m)/10), 30)}")


TZ_ORDER = ["Eastern", "Central", "Mountain", "Pacific"]


def plot_by_timezone_hour(rows: list[dict], label: str, out_path: Path) -> None:
    """Return % by local hour of purchase, one panel per timezone."""
    groups = {tz: [r for r in rows if r["tz_label"] == tz] for tz in TZ_ORDER}
    n_cols = sum(1 for g in groups.values() if g)
    if not n_cols:
        return

    fig, axes = plt.subplots(2, n_cols, figsize=(7 * n_cols, 9),
                              gridspec_kw={"height_ratios": [2, 1]}, squeeze=False)
    fig.suptitle(f"Return % by local hour — by timezone — {label}", fontsize=12)

    col = 0
    for tz in TZ_ORDER:
        grp = groups[tz]
        if not grp:
            continue
        hours   = np.array([r["local_hour"] for r in grp])
        returns = np.array([r["return_pct"] for r in grp])
        colours = np.array(["green" if r["return_pct"] > 0 else "red" if r["return_pct"] < 0 else "steelblue" for r in grp])
        _scatter_bar(axes[0][col], axes[1][col], hours, returns, colours, list(range(24)),
                     f"{tz} (n={len(grp)})", "Local hour of purchase")
        axes[0][col].set_xticks(range(0, 24, 2))
        axes[1][col].set_xticks(range(0, 24, 2))
        col += 1

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def plot_by_timezone_staleness(rows: list[dict], label: str, out_path: Path) -> None:
    """Return % by signal staleness, one panel per timezone."""
    groups = {tz: [r for r in rows if r["tz_label"] == tz] for tz in TZ_ORDER}
    all_bins = list(range(6))
    n_cols = sum(1 for g in groups.values() if g)
    if not n_cols:
        return

    fig, axes = plt.subplots(2, n_cols, figsize=(6 * n_cols, 9),
                              gridspec_kw={"height_ratios": [2, 1]}, squeeze=False)
    fig.suptitle(f"Return % by signal staleness — by timezone — {label}", fontsize=12)

    col = 0
    for tz in TZ_ORDER:
        grp = groups[tz]
        if not grp:
            continue
        stal    = np.array([min(int(r["staleness"]), 5) for r in grp])
        returns = np.array([r["return_pct"] for r in grp])
        colours = np.array(["green" if r["return_pct"] > 0 else "red" if r["return_pct"] < 0 else "steelblue" for r in grp])
        _scatter_bar(axes[0][col], axes[1][col], stal, returns, colours, all_bins,
                     f"{tz} (n={len(grp)})", "Hours since last forecast update")
        axes[0][col].set_xticks(all_bins)
        axes[0][col].set_xticklabels([f"{b}-{b+1}h" for b in all_bins], fontsize=8)
        axes[1][col].set_xticks(all_bins)
        axes[1][col].set_xticklabels([f"{b}-{b+1}h" for b in all_bins], fontsize=8)
        col += 1

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def _print_summary(rows: list[dict]) -> None:
    print(f"\n{len(rows)} closed positions")
    by_hour: dict[int, list] = defaultdict(list)
    for r in rows:
        by_hour[r["hour"]].append(r["return_pct"])
    print("\nMean return by UTC hour of purchase:")
    for h in sorted(by_hour):
        m = np.mean(by_hour[h])
        n = len(by_hour[h])
        sign = "-" if m < 0 else "+"
        print(f"  {h:02d}:xx  {m:+7.1f}%  n={n:3d}  {sign}{'█' * min(int(abs(m)/5), 30)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    explicit_csv = out_dir = start_filter = end_filter = None

    if "--csv" in args:
        i = args.index("--csv")
        explicit_csv = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
    if "--out" in args:
        i = args.index("--out")
        out_dir = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
    if "--start" in args:
        i = args.index("--start")
        start_filter = args[i + 1]
        args = args[:i] + args[i + 2:]
    if "--end" in args:
        i = args.index("--end")
        end_filter = args[i + 1]
        args = args[:i] + args[i + 2:]

    positional = [a for a in args if not a.startswith("--")]
    csv_path = explicit_csv or (find_csv(Path(positional[0])) if positional else find_csv(REPO_ROOT / "output"))
    print(f"Loading: {csv_path}")

    rows = load(csv_path)
    if start_filter:
        rows = [r for r in rows if r["buy_date"] >= start_filter]
    if end_filter:
        rows = [r for r in rows if r["buy_date"] <= end_filter + " 23:59"]
    if not rows:
        print("No closed positions found.")
        return

    out_dir = out_dir or csv_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    label = csv_path.parent.name
    if start_filter or end_filter:
        label += f" [{start_filter or ''}–{end_filter or ''}]"

    # Time-dimension plots
    plot_by_hour(rows, label, out_dir / "return_by_hour.png")
    plot_by_hour_and_day(rows, label, out_dir / "return_by_hour_by_day.png")
    plot_by_hour_and_day(rows, label, out_dir / "return_by_utc_hour_by_day.png", use_utc=True)
    plot_by_staleness(rows, label, out_dir / "return_by_staleness.png")

    # Edge and early-sell plots
    plot_return_vs_edge(rows, label, out_dir / "return_vs_edge.png")

    results = _load_results_cache()
    results.update(_load_settlements(csv_path))
    print(f"Loaded {len(results)} market results")
    sold_tickers = [r["symbol"] for r in rows if r["exit_type"] == "sold" and r["symbol"] not in results]
    if sold_tickers:
        print(f"Fetching {len(sold_tickers)} missing results from API...")
        fetched = _fetch_results_from_api(sold_tickers)
        results.update(fetched)
        print(f"  Got {len(fetched)} results")
    plot_early_sell(rows, results, label, out_dir / "early_sell_analysis.png")

    plot_by_timezone_hour(rows, label, out_dir / "return_by_timezone_hour.png")
    plot_by_timezone_staleness(rows, label, out_dir / "return_by_timezone_staleness.png")

    _print_summary(rows)


if __name__ == "__main__":
    main()
