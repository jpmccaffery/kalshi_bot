"""
kalshi_bot backtest demo — hourly synthetic data, mean-reversion strategy.

Generates realistic hourly probability paths for a mix of financial and
temperature markets, saves the input CSV, then runs the full kalshi_bot
pipeline (ProbMeanReversionRecommender + PaperTradingClient) and writes
the usual RunLogger output (ticks.csv, orders.csv, signals.csv, PNGs).

Usage (inside Docker)
---------------------
    python /app/demo_backtest.py
    python /app/demo_backtest.py --start 2024-01-02 --end 2024-03-01
    python /app/demo_backtest.py --balance 5000 --amount 200
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trading_bot.bot import Bot, BotConfig
from trading_bot.data_feed import BacktestDataFeed
from trading_bot.models import SizerConfig
from trading_bot.order_manager import SequencedOrderManager
from trading_bot.run_logger import RunLogger
from trading_bot.scheduling import FixedScheduleCalendar, always_open
from trading_bot.sell_engine import TimeBasedSellEngine
from trading_bot.sizer import FixedSizePositionSizer
from trading_bot.trading_client import PaperTradingClient

from kalshi_bot.recommender import ProbMeanReversionRecommender

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT        = Path(__file__).parent
INPUT_DIR   = ROOT / "demo_input"
OUTPUT_ROOT = ROOT / "demo_output" / "backtest"
TZ          = ZoneInfo("UTC")

# Tickers and their starting probability + OU parameters
# (theta = mean-reversion speed, sigma = hourly volatility)
MARKETS = {
    # Hourly financials
    "KXINXI":    dict(base=0.55, mu=0.50, theta=0.03, sigma=0.025),
    "KXEURUSDH": dict(base=0.48, mu=0.50, theta=0.04, sigma=0.020),
    "KXUSDJPYH": dict(base=0.52, mu=0.50, theta=0.04, sigma=0.020),
    "KXWTIH":    dict(base=0.44, mu=0.50, theta=0.03, sigma=0.030),
    # Daily temperatures (run hourly ticks but slower-moving)
    "KXHIGHTNYC": dict(base=0.60, mu=0.55, theta=0.02, sigma=0.012),
    "KXLOWTNYC":  dict(base=0.40, mu=0.45, theta=0.02, sigma=0.012),
}


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------

def generate_prices(start: date, end: date, rng: np.random.Generator) -> pd.DataFrame:
    """
    Hourly Ornstein-Uhlenbeck probability paths with a realistic bid/ask spread.

    Columns match KalshiDataFeed's provided_schema:
        symbol, ts, yes_bid, yes_ask, no_bid, no_ask, volume, open_interest
    """
    hours = pd.date_range(
        datetime(start.year, start.month, start.day, tzinfo=TZ),
        datetime(end.year,   end.month,   end.day,   tzinfo=TZ),
        freq="h",
        inclusive="left",
    )

    rows = []
    for ticker, params in MARKETS.items():
        p = params["base"]
        for ts in hours:
            # Ornstein-Uhlenbeck step
            p += params["theta"] * (params["mu"] - p) + rng.normal(0, params["sigma"])
            p  = float(np.clip(p, 0.03, 0.97))

            spread   = round(rng.uniform(0.01, 0.03), 2)
            yes_ask  = round(min(p + spread / 2, 0.99), 2)
            yes_bid  = round(max(p - spread / 2, 0.01), 2)
            no_ask   = round(min(1 - yes_bid, 0.99), 2)
            no_bid   = round(max(1 - yes_ask, 0.01), 2)

            rows.append({
                "symbol":        ticker,
                "ts":            ts,
                "yes_bid":       yes_bid,
                "yes_ask":       yes_ask,
                "no_bid":        no_bid,
                "no_ask":        no_ask,
                "volume":        int(rng.integers(50, 500)),
                "open_interest": int(rng.integers(100, 2_000)),
            })

    df = pd.DataFrame(rows).sort_values(["symbol", "ts"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="kalshi_bot hourly backtest demo")
    parser.add_argument("--start",   default="2024-01-02", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",     default="2024-02-01", help="End date YYYY-MM-DD")
    parser.add_argument("--balance", default=2_000.0, type=float, help="Starting balance ($)")
    parser.add_argument("--amount",  default=100.0,   type=float, help="Amount per trade ($)")
    parser.add_argument("--window",  default=24,      type=int,   help="Rolling mean window (bars)")
    parser.add_argument("--seed",    default=42,      type=int,   help="RNG seed")
    args = parser.parse_args()

    start   = date.fromisoformat(args.start)
    end     = date.fromisoformat(args.end)
    balance = Decimal(str(args.balance))
    amount  = Decimal(str(args.amount))
    rng     = np.random.default_rng(args.seed)

    run_log = RunLogger(base_dir=OUTPUT_ROOT, price_col="yes_ask")
    log     = run_log.log

    log.info("=" * 60)
    log.info("kalshi_bot backtest  —  hourly, mean-reversion strategy")
    log.info("  Tickers  : %s", list(MARKETS))
    log.info("  Period   : %s → %s", start, end)
    log.info("  Balance  : $%.2f   amount/trade: $%.2f", float(balance), float(amount))
    log.info("  Window   : %d bars", args.window)
    log.info("=" * 60)

    # Generate and save input data
    prices_df = generate_prices(start, end, rng)
    INPUT_DIR.mkdir(exist_ok=True)
    input_csv = INPUT_DIR / "kalshi_backtest_hourly.csv"
    prices_df.to_csv(input_csv, index=False)
    run_log.save_input(prices_df, "prices.csv")
    log.info("Generated %d hourly bars across %d tickers → %s",
             len(prices_df), len(MARKETS), input_csv)

    sizer_config = SizerConfig(
        fee_rate         = 0.010,
        slippage_rate    = 0.005,
        min_order_value  = Decimal("10"),
        max_position_pct = 0.40,
        price_column     = "yes_ask",
        limit_offset_pct = 0.005,
    )

    client = PaperTradingClient(
        initial_balance = balance,
        fee_rate        = 0.010,
        bid_column      = "yes_bid",
        ask_column      = "yes_ask",
    )

    calendar = FixedScheduleCalendar(
        times        = [time(h, 45) for h in range(24)],  # every hour at :45
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
                            symbols       = list(MARKETS),
                            lookback_bars = args.window * 3,
                            data_source   = input_csv,
                            tz            = TZ,
                        ),
        recommender   = ProbMeanReversionRecommender(
                            window    = args.window,
                            threshold = 0.05,
                        ),
        sizer         = FixedSizePositionSizer(
                            sizer_config,
                            amount_per_trade = amount,
                            max_positions    = len(MARKETS),
                        ),
        sizer_config  = sizer_config,
        sell_engine   = TimeBasedSellEngine(max_hold=timedelta(hours=48)),
        order_manager = SequencedOrderManager(),
        trading_client= client,
        dry_run       = False,
    )

    bot = Bot(config)

    for tick in calendar:
        result = bot.on_tick(tick)
        run_log.record(result, client)

    run_log.finalize(balance, client, prices_df, price_col="yes_ask")


if __name__ == "__main__":
    main()
