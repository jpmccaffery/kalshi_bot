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

| Variable | Description |
|---|---|
| `KALSHI_API_KEY_ID` | Key ID from the Kalshi dashboard |
| `KALSHI_API_PRIVATE_KEY_PATH` | Absolute path to your `kalshi_private.pem` |
| `KALSHI_DEMO` | `true` for demo API, `false` for live (default: `true`) |
| `KALSHI_MARKETS` | Comma-separated tickers, e.g. `KXINX-24,KXETHD-24` |
| `KALSHI_MARKET_PATTERN` | Regex alternative to `KALSHI_MARKETS`, e.g. `^KXINX-` |
| `TRADING_DRY_RUN` | `true` to skip real order submission (default: `true`) |

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
| `--output DIR` | Directory for CSV/PNG run output (default: `./output`) |

Example — one tick, dry run, custom output dir:

```bash
docker compose -f docker/docker-compose.yaml run --rm kalshi_bot --once --dry-run --output /app/output
```

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
│   ├── auth.py          # RSA-SHA256 request signing
│   ├── client.py        # KalshiTradingClient (order placement, balance, positions)
│   ├── config.py        # BotConfig factory — reads environment variables
│   ├── data_feed.py     # KalshiDataFeed + resolve_symbols()
│   ├── recommender.py   # ProbMeanReversionRecommender
│   └── run.py           # CLI entry point
├── scripts/
│   ├── _kalshi_api.py   # Shared auth + pagination helpers
│   ├── list_series.py
│   ├── list_events.py
│   └── list_markets.py
├── tests/
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yaml
│   └── requirements.txt
└── .env.example
```
