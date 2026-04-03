# kalshi_bot

Automated trading bot for [Kalshi](https://kalshi.com) prediction markets. Runs in Docker with a pluggable strategy and data-feed architecture. Starts in paper-trading mode (simulated fills, no real orders).

---

## Project structure

```
src/
├── main.py                  # main loop — wires everything together
├── config.py                # loads config.yaml + .env
├── kalshi/
│   ├── client.py            # REST API client with RSA auth
│   └── models.py            # Market, Signal, Fill, Position data classes
├── data/
│   └── base.py              # DataFeed interface + KalshiDataFeed
├── strategies/
│   ├── base.py              # Strategy interface
│   └── null_strategy.py    # logs markets, never trades (default)
└── execution/
    ├── base.py              # Executor interface + risk controls
    ├── paper_trader.py      # simulates fills, logs to logs/paper_fills.csv
    └── live_trader.py       # places real orders (off by default)

tests/
├── test_models.py
├── test_config.py
├── test_null_strategy.py
├── test_paper_trader.py
├── test_risk_controls.py
└── test_data_feed.py
```

---

## Setup

### 1. Kalshi API credentials

Generate an RSA key pair and register the public key at https://kalshi.com/account/api:

```bash
openssl genrsa -out kalshi_private.pem 2048
openssl rsa -in kalshi_private.pem -pubout -out kalshi_public.pem
```

Copy `.env.example` to `.env` and fill in your Key ID and private key path:

```bash
cp .env.example .env
```

### 2. Configure markets and strategy

Edit `config.yaml`:

- Set `kalshi.environment` to `demo` or `production`
- Add market tickers to `trading.markets` (find them at https://kalshi.com/markets)
- Set `trading.strategy` to the module name of your strategy (e.g. `null_strategy`)
- Adjust `risk` limits as needed
- Set `execution.mode` to `paper` (default) or `live`

### 3. Build the image

```bash
cd docker
docker compose build
```

---

## Running the bot

```bash
cd docker
docker compose up kalshi_bot
```

The bot polls the configured markets every `loop_interval_seconds`, passes market data to the active strategy, and routes any signals through the executor. In paper mode, fills are simulated and logged to `logs/paper_fills.csv`. To stop cleanly, send SIGINT (`Ctrl+C`) or `docker compose stop`.

---

## Running tests

```bash
cd docker
docker compose run --rm test
```

The `test` service mounts `src/` and `tests/` from your local machine, so changes to those directories are reflected immediately — no rebuild required. Only rebuild if you change `requirements.txt` or the `Dockerfile`:

```bash
docker compose build
```

---

## Adding a strategy

1. Create `src/strategies/my_strategy.py` with a class `MyStrategy` that extends `Strategy`:

```python
from src.kalshi.models import Market, Signal
from src.strategies.base import Strategy

class MyStrategy(Strategy):
    def generate_signals(self, markets: dict[str, Market]) -> list[Signal]:
        # your logic here
        return []
```

2. Set `trading.strategy: my_strategy` in `config.yaml`.

The strategy receives a fresh market snapshot each iteration and returns a list of `Signal` objects. Risk controls (position limits, daily loss cap) are applied by the executor before any order is placed.

---

## Going live

1. Run in paper mode until you are confident in the strategy's behavior.
2. Set `execution.mode: live` in `config.yaml`.
3. Ensure `kalshi.environment: production` and your `.env` points to a valid production API key.
4. Start with conservative `risk` limits.
