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

# run tests
docker compose -f docker/docker-compose.yaml run --rm test
```

## Environment variables

Copy `.env.example` to `.env` and fill in credentials before running live.
All config is via environment variables — no config files.
