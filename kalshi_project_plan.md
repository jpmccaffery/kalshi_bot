# Kalshi Trading Bot — Project Plan for Claude Code

## Context

The user (Jack) has built a generic algorithmic trading framework called
`trading_bot`. It is stable, well-tested (156 unit tests passing), and
should not be modified without asking first. The full framework context
is documented in `CLAUDE_CONTEXT.md` in the trading_bot repo.

The goal now is to create a **separate project** (`kalshi_bot`) that
imports `trading_bot` as a dependency and implements a working trading
loop against the Kalshi prediction-market exchange.

## Step 1: Set up CLAUDE.md files

### In the `trading_bot` repo, create `CLAUDE.md`:

This project is a stable framework. Do not modify source code in `src/`
or `tests/` without explicitly asking the user first and explaining what
change is needed and why. Minor fixes (typos, docstrings) are fine.

If a protocol change is needed to support a consumer project, propose
the change and wait for approval before implementing.

### In the new `kalshi_bot` repo, create `CLAUDE.md`:

This project is under active development. Feel free to create, modify,
and refactor code as needed to accomplish the task.

This project depends on the `trading_bot` framework (installed as a
package). Do NOT modify trading_bot source code — if a framework change
is needed, stop and tell me what you need changed and why so I can
handle it in the framework repo separately.

## Step 2: Create the kalshi_bot project

### Repo structure

```
kalshi_bot/
├── CLAUDE.md
├── pyproject.toml          # depends on trading_bot (git install)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yaml
│   └── requirements.txt
├── src/kalshi_bot/
│   ├── __init__.py
│   ├── client.py           # KalshiTradingClient (implements TradingClientProtocol)
│   ├── data_feed.py        # KalshiDataFeed (implements DataFeedProtocol)
│   ├── auth.py             # Kalshi RSA-PSS request signing
│   ├── recommender.py      # Kalshi-specific recommender(s)
│   ├── config.py           # BotConfig factory, env var loading
│   └── run.py              # main entry point, wires everything up
├── tests/
│   ├── conftest.py
│   ├── test_client.py
│   ├── test_data_feed.py
│   ├── test_auth.py
│   └── test_recommender.py
└── README.md
```

### Key dependency

Install `trading_bot` from its git repo:

```
pip install git+https://github.com/{user}/trading_bot.git@v1.0.0
```

or during development, install in editable mode from a local path:

```
pip install -e ../trading_bot
```

The Docker setup should mirror the trading_bot pattern (Python 3.11
slim, source mounted live, PYTHONPATH set).

## Step 3: Migrate existing Kalshi code

The trading_bot repo currently has exchange-specific Kalshi code in
`src/trading_bot/exchanges/kalshi.py`. This includes:

- RSA-SHA256 authentication (request signing)
- REST API client skeleton
- Kalshi-specific data structures

Copy this code into the kalshi_bot project as a starting point. It does
not need to stay in the trading_bot repo after migration.

There is also a `demo_kalshi.py` in the trading_bot repo with:

- Synthetic Ornstein-Uhlenbeck price data generation
- `ProbMeanReversionRecommender` (buys when price dips below rolling
  mean)
- A working backtest loop using `PaperTradingClient`

This is useful as reference for how the framework wires together for
Kalshi-style data.

## Step 4: Implement KalshiDataFeed

Implements `DataFeedProtocol` from trading_bot. Key points:

- **API base URLs**: demo is `https://demo-api.kalshi.co/trade-api/v2`,
  live is `https://trading-api.kalshi.com/trade-api/v2`
- **Market discovery**: `GET /markets` supports `status=open`,
  `minCloseTs`, `maxCloseTs`, `limit`, and cursor-based pagination
- **Price columns**: Kalshi markets have `yes_bid`, `yes_ask`, `no_bid`,
  `no_ask`, `volume`, `open_interest` — not OHLCV
- **provided_schema** should declare these columns
- **Timezone**: Kalshi trades 24/7, use `always_open` trading_days
  helper from the framework's scheduling module

The data feed should support both live API calls and a backtest mode
that reads from saved DataFrames (the framework's `BacktestDataFeed`
already handles this — the Kalshi feed is for live use).

## Step 5: Implement KalshiTradingClient

Implements `TradingClientProtocol` from trading_bot. Key points:

- Authentication uses RSA-PSS request signing with `KALSHI-ACCESS-KEY`,
  `KALSHI-ACCESS-SIGNATURE`, and `KALSHI-ACCESS-TIMESTAMP` headers
- Credentials from env vars: `KALSHI_API_KEY`, `KALSHI_PRIVATE_KEY`,
  `KALSHI_BASE_URL`
- `place_limit_order()` maps framework Order to Kalshi REST API order
  submission; `direction='long'` maps to yes side, `'short'` to no side
- `get_positions()` returns positions DataFrame with framework schema
- `get_balance()` returns Decimal balance from Kalshi account
- `update_prices()` is a no-op (live client gets real fills from the
  exchange)
- Start with the **demo environment** — no real money at risk

## Step 6: Build a run loop

Wire everything together in `run.py`:

```python
from trading_bot.bot import Bot, BotConfig
from trading_bot.models import SizerConfig
from trading_bot.scheduling import FixedScheduleCalendar, always_open
from trading_bot.sizer import FixedSizePositionSizer
from trading_bot.sell_engine import TimeBasedSellEngine
from trading_bot.order_manager import SequencedOrderManager
from trading_bot.run_logger import RunLogger

from kalshi_bot.data_feed import KalshiDataFeed
from kalshi_bot.client import KalshiTradingClient
from kalshi_bot.recommender import SomeKalshiRecommender
```

Start simple — Level 1 from the framework spec:
- `FixedSizePositionSizer` (flat dollar amount per trade)
- `TimeBasedSellEngine` or `NullSellEngine`
- `dry_run=True` initially
- Use `RunLogger` for output (it already handles CSV, plots, logging)

## Step 7: Test against Kalshi demo

Once the client and data feed are working:

1. Run with `dry_run=True` first — verify data flows, signals generate,
   orders construct correctly
2. Switch to `dry_run=False` against the demo API — verify real
   (simulated) order placement
3. Check `RunLogger` output: signals.csv, orders.csv, ticks.csv for
   correctness

## Implementation order

1. Project scaffolding + CLAUDE.md files + dependency setup
2. Auth module (RSA-PSS signing) + tests
3. KalshiDataFeed (market discovery + snapshot fetch) + tests
4. KalshiTradingClient (order placement + position/balance queries) + tests
5. Simple recommender (can reuse ProbMeanReversionRecommender or similar)
6. Run loop wiring
7. Dry run validation
8. Live demo API testing

Each step should have passing tests before moving to the next.
