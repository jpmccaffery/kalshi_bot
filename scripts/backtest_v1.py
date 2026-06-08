"""
Backtest TemperatureRecommenderV1 against historical market snapshot and
weather forecast parquets.

Usage (inside Docker):
    python scripts/backtest_v1.py
    python scripts/backtest_v1.py --start 2026-05-25 --end 2026-06-01
    python scripts/backtest_v1.py --tick-minute 50 --max-positions 15
    python scripts/backtest_v1.py --name bt_tight --max-staleness 2

Output goes to output/<name>/ (default: output/backtest_v1/).
Produces the same CSVs as a live run (orders, signals, settlements, etc.)
so plot_analysis.py works on it directly.
"""
from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.dataset as _ds

from trading_bot.bot import Bot, BotConfig
from trading_bot.models import SizerConfig
from trading_bot.order_manager import SequencedOrderManager
from trading_bot.run_logger import RunLogger
from trading_bot.scheduling import FixedScheduleCalendar, always_open

from kalshi_bot.paper_client import PaperTradingClient
from kalshi_bot.recommenders.v1_historical import TemperatureRecommenderV1Historical
from kalshi_bot.sell_engine import ModelBasedSellEngine
from kalshi_bot.sizer import PaddedSizer
from trading_bot.data_feed import BacktestDataFeed
from trading_bot.sizer import FixedSizePositionSizer

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
TZ       = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Settlement client
# ---------------------------------------------------------------------------

class BacktestPaperClient(PaperTradingClient):
    """
    PaperTradingClient that settles from local market_results parquet
    instead of making live Kalshi API calls.

    Call set_time(tick.ts) each tick so _settle_expired only settles
    markets whose close_time has passed in simulation time.
    """

    def __init__(self, results: dict[str, tuple[str, dt.datetime]], **kwargs) -> None:
        super().__init__(**kwargs)
        # ticker -> (result, close_time)
        self._results     = results
        self._current_time: dt.datetime | None = None

    def set_time(self, ts: dt.datetime) -> None:
        self._current_time = ts

    def _settle_expired(self) -> None:
        if self._current_time is None:
            return
        to_settle = []
        for ticker in list(self._positions):
            if ticker not in self._results:
                continue
            result, close_time = self._results[ticker]
            if self._current_time >= close_time:
                to_settle.append((ticker, result, close_time))

        for ticker, result, close_time in to_settle:
            pos         = self._positions.pop(ticker)
            qty         = pos["qty"]
            cost        = pos["cost_basis"]
            kalshi_side = pos.get("kalshi_side", "yes")
            ts_str      = close_time.strftime("%Y-%m-%d %H:%M")

            if result == "void":
                payout         = cost
                pnl            = Decimal("0")
                self._balance += payout
                self._write_settlement(ts_str, ticker, "void", qty, cost, payout, pnl)
            elif (result == "yes" and kalshi_side == "yes") or \
                 (result == "no"  and kalshi_side == "no"):
                payout         = qty * Decimal("1.00")
                pnl            = payout - cost
                self._balance += payout
                self._write_settlement(ts_str, ticker, result, qty, cost, payout, pnl)
            else:
                payout         = Decimal("0")
                pnl            = -cost
                self._write_settlement(ts_str, ticker, result, qty, cost, payout, pnl)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_snapshots(start: dt.date, end: dt.date, tick_minute: int) -> pd.DataFrame:
    """
    Load market snapshots, rename to BacktestDataFeed column names,
    and resample to exact :MM ticks using forward-fill.
    """
    d = _ds.dataset(DATA_DIR / "market" / "market_snapshots", format="parquet")
    df = d.to_table().to_pandas()
    df["ts"] = pd.to_datetime(df["poll_time"], utc=True)
    df = df.rename(columns={"ticker": "symbol"})

    # Restrict to date range (generous: include day before to seed ffill)
    start_ts = pd.Timestamp(start, tz="UTC") - pd.Timedelta(days=1)
    end_ts   = pd.Timestamp(end,   tz="UTC") + pd.Timedelta(days=1)
    df = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)]

    keep = ["symbol", "ts", "yes_bid", "yes_ask", "no_bid", "no_ask",
            "volume", "open_interest", "status"]
    df = df[[c for c in keep if c in df.columns]].copy()

    # Build regular :MM tick grid
    tick_start = pd.Timestamp(start, tz="UTC")
    tick_end   = pd.Timestamp(end,   tz="UTC") + pd.Timedelta(days=1)
    grid = pd.date_range(
        tick_start.replace(hour=0, minute=tick_minute, second=0, microsecond=0),
        tick_end,
        freq="1h",
    )
    grid = grid[(grid >= tick_start) & (grid <= tick_end)]

    # Resample each symbol onto the grid via ffill
    symbols = df["symbol"].unique()
    resampled = []
    for sym in symbols:
        sdf = df[df["symbol"] == sym].set_index("ts").sort_index()
        # Reindex to grid, forward-fill prices, drop rows with no price yet
        numeric_cols = ["yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "open_interest"]
        sdf_num = sdf[[c for c in numeric_cols if c in sdf.columns]]
        sdf_grid = sdf_num.reindex(sdf_num.index.union(grid)).sort_index()
        sdf_grid = sdf_grid.ffill().reindex(grid).dropna(subset=["yes_ask"])
        sdf_grid["symbol"] = sym
        sdf_grid.index.name = "ts"
        resampled.append(sdf_grid.reset_index())

    result = pd.concat(resampled, ignore_index=True)
    result["ts"] = pd.to_datetime(result["ts"], utc=True)
    return result.sort_values(["ts", "symbol"]).reset_index(drop=True)


def load_results() -> dict[str, tuple[str, dt.datetime]]:
    """Return {ticker: (result, close_time)} for all finalized markets."""
    d = _ds.dataset(DATA_DIR / "market" / "market_results", format="parquet")
    df = d.to_table(columns=["ticker", "result", "close_time"]).to_pandas()
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True)
    df = df.drop_duplicates("ticker")
    return {
        row["ticker"]: (row["result"], row["close_time"].to_pydatetime())
        for _, row in df.iterrows()
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="V1 historical backtest")
    parser.add_argument("--start",         default=None,
                        help="Start date YYYY-MM-DD (default: earliest snapshot date)")
    parser.add_argument("--end",           default=None,
                        help="End date YYYY-MM-DD (default: latest snapshot date)")
    parser.add_argument("--tick-minute",   default=50, type=int,
                        help="Minute of hour for ticks (default: 50)")
    parser.add_argument("--max-positions", default=15, type=int,
                        help="Max simultaneous positions (default: 15)")
    parser.add_argument("--amount",        default=10.0, type=float,
                        help="Amount per trade in $ (default: 10)")
    parser.add_argument("--balance",       default=10000.0, type=float,
                        help="Starting paper balance (default: 10000)")
    parser.add_argument("--max-staleness", default=None, type=float,
                        help="Max NBM data age in hours (default: no limit)")
    parser.add_argument("--name",          default="backtest_v1",
                        help="Output subdirectory name (default: backtest_v1)")
    args = parser.parse_args()

    # Determine date range from data if not specified
    if args.start or args.end:
        start = dt.date.fromisoformat(args.start) if args.start else None
        end   = dt.date.fromisoformat(args.end)   if args.end   else None
    else:
        start = end = None

    if start is None or end is None:
        d = _ds.dataset(DATA_DIR / "market" / "market_snapshots", format="parquet")
        ts_col = d.to_table(columns=["poll_time"]).to_pandas()
        ts_col["poll_time"] = pd.to_datetime(ts_col["poll_time"], utc=True)
        if start is None:
            start = ts_col["poll_time"].min().date()
        if end is None:
            end = ts_col["poll_time"].max().date()

    print(f"Backtest period: {start} → {end}")
    print(f"Tick: :{args.tick_minute:02d}  max_positions={args.max_positions}  "
          f"amount=${args.amount}  max_staleness={args.max_staleness}h")

    out_dir = ROOT / "output" / args.name
    run_log = RunLogger(base_dir=out_dir, price_col="yes_ask")

    print("Loading market snapshots...")
    prices_df = load_snapshots(start, end, args.tick_minute)
    symbols   = sorted(prices_df["symbol"].unique().tolist())
    print(f"  {len(prices_df):,} rows, {len(symbols)} symbols")

    print("Loading market results...")
    results = load_results()
    print(f"  {len(results)} finalized markets")

    recommender = TemperatureRecommenderV1Historical(
        data_dir            = DATA_DIR,
        max_staleness_hours = args.max_staleness,
        min_edge            = 0.10,
        min_yes_ask         = 0.10,
        max_per_expiry      = 1,
        output_dir          = out_dir / run_log.run_dir.name,
    )

    client = BacktestPaperClient(
        results          = results,
        starting_balance = Decimal(str(args.balance)),
        output_dir       = out_dir / run_log.run_dir.name,
    )

    sizer_config = SizerConfig(
        fee_rate          = 0.035,
        slippage_rate     = 0.005,
        min_order_value   = Decimal("1"),
        max_position_pct  = 0.40,
        price_column      = "yes_ask",
        sell_price_column = "yes_bid",
        limit_offset_pct  = 0.005,
    )

    calendar = FixedScheduleCalendar(
        times        = [dt.time(h, args.tick_minute) for h in range(24)],
        start        = start,
        end          = end,
        mode         = "backtest",
        tz           = TZ,
        trading_days = always_open,
    )

    config = BotConfig(
        tz            = TZ,
        calendar      = calendar,
        data_feed     = BacktestDataFeed(
                            symbols      = symbols,
                            lookback_bars = 30,
                            data_source  = prices_df,
                            tz           = TZ,
                        ),
        recommender   = recommender,
        sizer         = PaddedSizer(
                            FixedSizePositionSizer(
                                sizer_config,
                                amount_per_trade = Decimal(str(args.amount)),
                                max_positions    = args.max_positions,
                            ),
                            flat_padding = Decimal("0.01"),
                        ),
        sizer_config  = sizer_config,
        sell_engine   = ModelBasedSellEngine(recommender, output_dir=out_dir / run_log.run_dir.name),
        order_manager = SequencedOrderManager(),
        trading_client= client,
        dry_run       = False,
    )

    bot  = Bot(config)
    tick_count = 0
    for tick in calendar:
        client.set_time(tick.ts)
        result = bot.on_tick(tick)
        run_log.record(result, client)
        tick_count += 1
        if tick_count % 24 == 0:
            print(f"  {tick.ts.date()}  balance=${float(client.get_balance()):.2f}")

    # Pass None for prices_df — trades.png generates one subplot per symbol
    # which would be hundreds of subplots for temperature markets.
    run_log.finalize(Decimal(str(args.balance)), client, None, price_col="yes_ask")
    print(f"\nDone. Output: {out_dir / run_log.run_dir.name}")


if __name__ == "__main__":
    main()
