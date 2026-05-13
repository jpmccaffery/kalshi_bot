"""
Configuration loader for kalshi_bot.

All config is driven by environment variables (loaded from .env if present).

Environment variables
---------------------
KALSHI_API_KEY_ID           Kalshi API key ID                       (required)
KALSHI_API_PRIVATE_KEY_PATH Path to RSA private key PEM file        (required)
KALSHI_DEMO                 "true"/"false" — use demo API           (default true)
KALSHI_MARKETS              Comma-separated explicit market tickers  (use if you know exact tickers)
KALSHI_SERIES               Comma-separated series tickers           (recommended — resolves via events)
                            e.g. "KXINX,KXEURUSD,KXWTI"
                            KALSHI_MARKETS takes precedence if set.

TRADING_AMOUNT_PER_TRADE    Fixed dollar amount per buy order        (default 100)
TRADING_DRY_RUN             "true"/"false" — skip real order submit  (default true)
TRADING_MAX_POSITIONS       Maximum simultaneous positions           (default 3)
TRADING_MAX_HOLD_HOURS      TimeBasedSellEngine max hold in hours    (default 6)
PAPER_BALANCE               Starting virtual balance for paper trading (default 10000)
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from trading_bot.bot import BotConfig
from trading_bot.models import SizerConfig
from trading_bot.order_manager import SequencedOrderManager
from trading_bot.scheduling import FixedScheduleCalendar, always_open
from trading_bot.models import Tick as _Tick
from typing import Iterator as _Iterator

from kalshi_bot.sell_engine import ModelBasedSellEngine
from kalshi_bot.sizer import PaddedSizer
from trading_bot.sizer import FixedSizePositionSizer

from kalshi_bot.client import KalshiTradingClient
from kalshi_bot.data_feed import KalshiDataFeed
from kalshi_bot.paper_client import PaperTradingClient
from kalshi_bot.temp_recommender import TemperatureRecommender


class _FutureOnlyCalendar(FixedScheduleCalendar):
    """FixedScheduleCalendar that skips ticks already in the past at startup."""

    def __iter__(self) -> _Iterator[_Tick]:
        now = datetime.now(tz=self._tz)
        for tick in super().__iter__():
            if tick.ts >= now:
                yield tick


def load_env(env_path: str | Path | None = None) -> None:
    """Load .env file if present.  Safe to call even if .env doesn't exist."""
    load_dotenv(dotenv_path=env_path, override=True)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable {name!r} is not set. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _bool_env(name: str, default: bool = True) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


def build_bot_config(tz: ZoneInfo = ZoneInfo("UTC"),
                     output_dir=None) -> "tuple[BotConfig, KalshiTradingClient, TemperatureRecommender, KalshiDataFeed]":
    """
    Build and return a (BotConfig, client) pair from environment variables.

    Returns the client separately so the caller can inspect balance etc.
    """
    load_env()

    key_id   = _require("KALSHI_API_KEY_ID")
    key_path = _require("KALSHI_API_PRIVATE_KEY_PATH")
    demo     = _bool_env("KALSHI_DEMO", default=True)

    markets_raw = os.environ.get("KALSHI_MARKETS", "").strip()
    series_raw  = os.environ.get("KALSHI_SERIES",  "").strip()

    if markets_raw:
        symbols       = [m.strip() for m in markets_raw.split(",") if m.strip()]
        series        = []
    elif series_raw:
        series        = [s.strip() for s in series_raw.split(",") if s.strip()]
        symbols       = []   # discovered each tick by KalshiDataFeed
    else:
        raise EnvironmentError(
            "Set KALSHI_MARKETS (explicit tickers) or "
            "KALSHI_SERIES (series tickers, e.g. KXINX,KXEURUSD) to specify markets."
        )

    dry_run          = _bool_env("TRADING_DRY_RUN", default=True)
    amount_per_trade = Decimal(os.environ.get("TRADING_AMOUNT_PER_TRADE", "100"))
    max_positions    = int(os.environ.get("TRADING_MAX_POSITIONS", "3"))
    paper_balance    = Decimal(os.environ.get("PAPER_BALANCE", "10000"))
    limit_padding    = Decimal(os.environ.get("TRADING_LIMIT_PADDING_CENTS", "1")) / 100

    sizer_config = SizerConfig(
        fee_rate          = 0.035,      # Kalshi taker fee: 7¢×C×(1-C), ~3.5% at 50¢
        slippage_rate     = 0.005,
        min_order_value   = Decimal("1"),
        max_position_pct  = 0.40,
        price_column      = "yes_ask",
        sell_price_column = "yes_bid",
        limit_offset_pct  = 0.005,
    )

    if dry_run:
        client = PaperTradingClient(
            starting_balance = paper_balance,
            key_id           = key_id,
            private_key_path = key_path,
            demo             = demo,
            tz               = tz,
            output_dir       = output_dir,
        )
    else:
        client = KalshiTradingClient(
            key_id           = key_id,
            private_key_path = key_path,
            demo             = demo,
            tz               = tz,
        )

    data_feed = KalshiDataFeed(
        symbols          = symbols,
        series_tickers   = series,
        key_id           = key_id,
        private_key_path = key_path,
        lookback_bars    = 30,
        demo             = demo,
        tz               = tz,
    )

    recommender = TemperatureRecommender(
        min_edge       = 0.10,
        min_yes_ask    = 0.10,
        max_per_expiry = 1,
        output_dir     = output_dir,
    )

    bot_config = BotConfig(
        tz            = tz,
        calendar      = _FutureOnlyCalendar(
                            times        = [__import__("datetime").time(h, 50) for h in range(24)],
                            start        = date.today(),
                            end          = date.today() + timedelta(days=30),
                            mode         = "live",
                            tz           = tz,
                            trading_days = always_open,
                        ),
        data_feed     = data_feed,
        recommender   = recommender,
        sizer         = PaddedSizer(
                            FixedSizePositionSizer(
                                sizer_config,
                                amount_per_trade = amount_per_trade,
                                max_positions    = max_positions,
                            ),
                            flat_padding = limit_padding,
                        ),
        sizer_config  = sizer_config,
        sell_engine   = ModelBasedSellEngine(recommender, output_dir=output_dir),
        order_manager = SequencedOrderManager(),
        trading_client= client,
        dry_run       = False,  # PaperTradingClient handles simulation; real client used only when dry_run=False
    )

    return bot_config, client, recommender, data_feed
