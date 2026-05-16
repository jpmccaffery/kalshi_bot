# kalshi_bot — Claude instructions

This project is under active development. Feel free to create, modify, and
refactor code as needed to accomplish the task.

This project depends on the `trading_bot` framework (mounted at
`/trading_bot` in Docker, available via PYTHONPATH). Do NOT modify
trading_bot source code. If a framework change is needed, stop and explain
what is needed and why so it can be handled in the framework repo separately.

## Running things

```bash
# interactive shell
docker compose -f docker/docker-compose.yaml run --rm --entrypoint bash kalshi_bot

# run the bot (dry run by default)
docker compose -f docker/docker-compose.yaml run --rm kalshi_bot

# named instance — prefixes every log line, writes to output/<name>/run_*/
docker compose -f docker/docker-compose.yaml run --rm kalshi_bot --name paper

# single tick (ALWAYS set TRADING_DRY_RUN=true when smoke-testing new code)
docker compose -f docker/docker-compose.yaml run --rm -e TRADING_DRY_RUN=true kalshi_bot --once

# run tests
docker compose -f docker/docker-compose.yaml run --rm test
```

## Environment variables

Copy `.env.example` to `.env` and fill in credentials before running live.
All config is via environment variables — no config files.

## Signal direction and YES/NO order flow

Signals carry `direction="long"` (buy YES) or `direction="short"` (buy NO).

**How a short signal becomes a NO order:**

1. `TemperatureRecommender` emits a `Signal` with `direction="short"` and
   `metadata={"kalshi_side": "no", "no_ask": ..., ...}` when
   `(1 - model_prob) - no_ask - fee >= min_edge`.
2. `PaddedSizer` detects short symbols, copies the prices DataFrame and
   swaps `yes_ask ← no_ask` for those rows, then passes everything to
   `FixedSizePositionSizer` so it prices NO orders correctly. After sizing,
   it patches the resulting `Order.metadata` with `{"kalshi_side": "no"}`.
3. `KalshiTradingClient.place_limit_order` reads
   `order.metadata.get("kalshi_side", "yes")` and submits
   `{"side": "no", "no_price": <cents>}` to the Kalshi API.
4. `PaperTradingClient` stores `kalshi_side` per position and inverts
   settlement: NO holders receive $1/contract when `result == "no"`.
5. `ModelBasedSellEngine` calls `recommender.get_position_side(ticker)` to
   know the direction, then uses `no_bid` and `1 - model_prob` for NO
   positions instead of `yes_bid` and `model_prob`.

Per group `(city, expiry, kind)`, only the single best edge opportunity
(YES or NO) is selected — the bot never holds both sides of the same contract.

## Instance labeling

Pass `--name <label>` (or set `BOT_NAME=<label>` in the environment) to:
- Prepend `[label]` to every log line
- Write output to `output/<label>/run_*/` instead of `output/run_*/`

This allows a paper instance and the live bot to run simultaneously with
fully separated logs and output directories.
