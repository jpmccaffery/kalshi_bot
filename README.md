# kalshi_bot

Automated trading bot for [Kalshi](https://kalshi.com) prediction markets, built on the `trading_bot` framework. Runs entirely in Docker. Defaults to paper trading (dry run, no real orders placed).

---

## Prerequisites

- Docker and Docker Compose installed
- The `trading_bot` repo cloned as a sibling directory:
  ```
  projects/
  ├── trading_bot/   ← framework (must exist)
  └── kalshi_bot/    ← this repo
  ```
- A Kalshi API key (see [Credentials](#credentials) below)

---

## Credentials

Generate an RSA-2048 key pair and register the public key at https://kalshi.com/account/api:

```bash
openssl genrsa -out kalshi_private.pem 2048
openssl rsa -in kalshi_private.pem -pubout -out kalshi_public.pem
```

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Key variables:

| Variable | Description | Default |
|---|---|---|
| `KALSHI_API_KEY_ID` | Key ID from the Kalshi dashboard | *(required)* |
| `KALSHI_API_PRIVATE_KEY_PATH` | Path to your `kalshi_private.pem` | *(required)* |
| `KALSHI_DEMO` | `true` for demo API, `false` for live | `true` |
| `KALSHI_SERIES` | Comma-separated series tickers, e.g. `KXLOWTLAX,KXHIGHDEN` | *(required)* |
| `KALSHI_MARKETS` | Comma-separated explicit market tickers (overrides `KALSHI_SERIES`) | — |
| `TRADING_DRY_RUN` | `true` = paper trading, `false` = live orders | `true` |
| `TRADING_AMOUNT_PER_TRADE` | Fixed dollar amount per buy order | `100` |
| `TRADING_MAX_POSITIONS` | Max simultaneous open positions | `3` |
| `TRADING_LIMIT_PADDING_CENTS` | Flat padding added to every buy limit price (cents) | `1` |
| `TRADING_SIDES` | Which contract sides to trade: `yes`, `no`, or `yes,no` | `yes,no` |
| `TRADING_TICK_MINUTE` | Minute past each hour to fire a tick (0–59) | `50` |
| `PAPER_BALANCE` | Starting virtual balance for paper trading | `10000` |
| `BOT_NAME` | Instance label — prefixes log lines, scopes output directory | — |

---

## Build the image

Run once, and again any time `docker/requirements.txt` or the `Dockerfile` changes:

```bash
docker compose -f docker/docker-compose.yaml build
```

---

## Drop into an interactive shell

The fastest way to explore, run scripts, or debug:

```bash
docker compose -f docker/docker-compose.yaml run --rm --entrypoint bash kalshi_bot
```

You'll land at `/app` with both `kalshi_bot` and `trading_bot` source trees live-mounted — edits on your host are reflected immediately, no rebuild needed.

---

## Run the bot

```bash
docker compose -f docker/docker-compose.yaml run --rm kalshi_bot
```

Flags (append after `kalshi_bot`):

| Flag | Description |
|---|---|
| `--dry-run` | Override `.env`, skip real order submission |
| `--once` | Run one tick then exit (useful for testing) |
| `--name LABEL` | Instance label: prefixes every log line with `[LABEL]` and writes output to `output/LABEL/run_*/`. Also readable from `BOT_NAME` env var. |
| `--output DIR` | Base directory for run output (default: `./output`) |

**Running a paper instance alongside live:**

```bash
# Live bot (already running)
docker compose -f docker/docker-compose.yaml run --rm kalshi_bot --name live

# Paper instance in a separate terminal
docker compose -f docker/docker-compose.yaml run --rm \
  -e TRADING_DRY_RUN=true \
  -e TRADING_MAX_POSITIONS=15 \
  kalshi_bot --name paper
```

Output goes to `output/live/run_*/` and `output/paper/run_*/` respectively.
Log lines are prefixed `[live]` and `[paper]` for easy distinction when tailing both.

> **Note:** Always pass `-e TRADING_DRY_RUN=true` or `--dry-run` when smoke-testing
> new code with `--once`. The live API is used by default.

---

## Run the tests

```bash
docker compose -f docker/docker-compose.yaml run --rm test
```

Source and tests are live-mounted, so no rebuild is needed after editing `src/` or `tests/`. Only rebuild if `docker/requirements.txt` or the `Dockerfile` changes.

---

## Utility scripts

Run from inside the interactive shell (`cd /app`):

### List series (top-level groupings)

```bash
python scripts/list_series.py
python scripts/list_series.py --output series.csv
python scripts/list_series.py --live --output series.csv
```

### List events (question instances within a series)

```bash
python scripts/list_events.py
python scripts/list_events.py --series KXINX
python scripts/list_events.py --series KXINX --output events.csv
```

### List markets (tradeable contracts)

```bash
python scripts/list_markets.py
python scripts/list_markets.py --series KXINX
python scripts/list_markets.py --series KXINX --output markets.csv

# Print tickers only — useful for building KALSHI_MARKETS in .env:
python scripts/list_markets.py --series KXINX --tickers-only
```

All scripts default to the demo API. Pass `--live` to hit the live endpoint, or `--demo` to force demo regardless of `KALSHI_DEMO` in `.env`.

---

## Project layout

```
kalshi_bot/
├── src/kalshi_bot/
│   ├── auth.py              # RSA-SHA256 request signing
│   ├── client.py            # KalshiTradingClient — live order placement
│   ├── config.py            # BotConfig factory — reads environment variables
│   ├── data_feed.py         # KalshiDataFeed — price fetching, market discovery
│   ├── paper_client.py      # PaperTradingClient — simulated trading + settlement
│   ├── sell_engine.py       # ModelBasedSellEngine — exit when bid > model EV
│   ├── sizer.py             # PaddedSizer — wraps FixedSizePositionSizer, handles NO orders
│   ├── temp_recommender.py  # TemperatureRecommender — YES and NO signal generation
│   └── run.py               # CLI entry point (--name, --once, --dry-run)
│   └── forecast/
│       ├── distribution.py  # Piecewise-linear CDF from NBM percentiles (capped at 0.97)
│       ├── nbm_client.py    # NOAA NBM bulletin parser
│       ├── nws_client.py    # NWS API sanity-check forecast
│       ├── recommender.py   # EdgeRow scoring (YES edge + NO edge per contract)
│       └── stations.py      # City → ICAO station mapping
├── scripts/
│   ├── daily_pnl.py         # PnL by expiry date across all runs
│   ├── transaction_summary.py  # Per-ticker entry/exit/PnL table
│   ├── plot_edge_vs_pnl.py  # Scatter: edge vs PnL
│   ├── replay.py            # Backtest signal selection strategies
│   └── list_markets.py      # Discover series/markets via Kalshi API
├── tests/
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yaml
│   └── requirements.txt
├── output/                  # Run directories (git-ignored)
│   ├── run_YYYYMMDD_HHMMSS/ # Live bot runs
│   └── <name>/run_*/        # Named instance runs (e.g. output/paper/)
└── .env.example
```

## YES and NO orders

The bot evaluates both sides of every contract each tick. A **YES** signal (`direction="long"`) fires when `model_prob - yes_ask - fee >= min_edge`. A **NO** signal (`direction="short"`) fires when `(1 - model_prob) - no_ask - fee >= min_edge`. Only the better edge wins the slot when both are positive on the same contract.

NO orders reach Kalshi as `{"action": "buy", "side": "no", "no_price": <cents>}`. Settlement for NO holders is inverted: payout = $1/contract when `result == "no"`.
