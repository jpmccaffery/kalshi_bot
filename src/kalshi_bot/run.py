"""
kalshi_bot main entry point.

Usage
-----
    python -m kalshi_bot               # live loop, uses .env config
    python -m kalshi_bot --dry-run     # same but never submits orders
    python -m kalshi_bot --once        # run a single tick then exit
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.bot import Bot, TickResult
from trading_bot.models import Tick
from trading_bot.run_logger import RunLogger

from kalshi_bot.config import build_bot_config, load_env
from kalshi_bot.paper_client import PaperTradingClient

ROOT       = Path(__file__).parent.parent.parent   # repo root
OUTPUT_DIR = ROOT / "output"

log = logging.getLogger("kalshi_bot")

_EV_FIELDNAMES = [
    "date", "n_signals", "n_buys", "n_sells", "n_positions", "n_errors",
    "cash", "market_value_bid", "model_ev",
]


def _append_ev_tick(
    path: Path,
    date_str: str,
    client,
    bars: pd.DataFrame,
    recommender,
    result: "TickResult",
) -> None:
    """Write one row combining tick activity counts with portfolio valuation."""
    positions = client.get_positions()
    cash = float(client.get_balance())

    market_value = cash
    model_ev = cash

    for _, pos in positions.iterrows():
        symbol = pos["symbol"]
        qty    = float(pos["quantity"])

        bid = float("nan")
        if not bars.empty and "symbol" in bars.columns:
            bar = bars[bars["symbol"] == symbol]
            if not bar.empty and "yes_bid" in bar.columns:
                bid = float(bar.iloc[0]["yes_bid"] or 0)

        if bid == bid:  # not NaN
            market_value += qty * bid

        prob = recommender.get_model_prob(symbol)
        if prob is not None:
            model_ev += qty * prob
        elif bid == bid:
            model_ev += qty * bid  # fallback: treat bid as EV when no model data

    write_header = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_EV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "date":             date_str,
            "n_signals":        len(result.signals),
            "n_buys":           len(result.buy_orders),
            "n_sells":          len(result.sell_orders),
            "n_positions":      len(positions),
            "n_errors":         len(result.errors),
            "cash":             round(cash, 4),
            "market_value_bid": round(market_value, 4),
            "model_ev":         round(model_ev, 4),
        })


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi trading bot")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build orders but do not submit them to Kalshi",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Execute a single tick and exit (useful for debugging)",
    )
    parser.add_argument(
        "--output", default=str(OUTPUT_DIR),
        help="Directory for run logs and CSV output",
    )
    args = parser.parse_args()

    # Allow --dry-run flag to override env var
    if args.dry_run:
        os.environ["TRADING_DRY_RUN"] = "true"

    load_env()

    tz         = ZoneInfo("UTC")
    run_logger = RunLogger(base_dir=args.output, price_col="yes_bid")
    logger     = run_logger.log

    logger.info("=" * 60)
    logger.info("kalshi_bot starting")
    logger.info("  dry_run : %s", os.environ.get("TRADING_DRY_RUN", "true"))
    logger.info("  markets : %s", (
        os.environ.get("KALSHI_MARKETS")
        or os.environ.get("KALSHI_SERIES")
        or "(not set)"
    ))
    logger.info("  output  : %s", args.output)
    logger.info("=" * 60)

    try:
        bot_config, client, recommender, data_feed = build_bot_config(
            tz=tz, output_dir=run_logger.run_dir)
    except EnvironmentError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    initial_balance = client.get_balance()
    logger.info("Account balance: $%.4f", float(initial_balance))

    bot      = Bot(bot_config)
    calendar = bot_config.calendar

    # Graceful shutdown on SIGINT / SIGTERM.
    # _in_tick tracks whether we are currently executing a tick.
    # If a signal arrives during the inter-tick sleep, raise KeyboardInterrupt
    # to break out of the sleep immediately.  If it arrives mid-tick, just set
    # the flag and let the tick finish naturally before stopping.
    _stop    = [False]
    _in_tick = [False]

    def _handle_signal(sig, frame):
        _stop[0] = True
        if not _in_tick[0]:
            logger.info("Shutdown signal received — stopping immediately")
            raise KeyboardInterrupt
        logger.info("Shutdown signal received — finishing current tick")

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        ev_path = run_logger.run_dir / "ev_ticks.csv"

        def _pin_paper_positions() -> None:
            if isinstance(client, PaperTradingClient):
                data_feed.pin_tickers(client.held_tickers())

        if args.once:
            # Fire immediately at the current time, no waiting
            now  = datetime.now(tz=tz)
            tick = Tick(ts=now, index=0, is_final=True)
            logger.info("--once: firing immediately at %s", now.strftime("%H:%M:%S"))
            result = bot.on_tick(tick)
            run_logger.record(result, client)
            _append_ev_tick(ev_path, now.strftime("%Y-%m-%d %H:%M"),
                            client, result.snapshot.bars, recommender, result)
            _pin_paper_positions()
        else:
            for tick in calendar:
                if _stop[0]:
                    break
                _in_tick[0] = True
                result = bot.on_tick(tick)
                run_logger.record(result, client)
                _append_ev_tick(ev_path, tick.ts.strftime("%Y-%m-%d %H:%M"),
                                client, result.snapshot.bars, recommender, result)
                _pin_paper_positions()
                _in_tick[0] = False
                if _stop[0]:
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass  # clean shutdown from sleep period
    except Exception as exc:
        logger.error("Fatal error in tick loop: %s", exc, exc_info=True)
    finally:
        run_logger.finalize(initial_balance, client)
        logger.info("kalshi_bot stopped")


if __name__ == "__main__":
    main()
