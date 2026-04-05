import importlib
import logging
import signal
import sys
import time

from src.config import load_config
from src.execution.paper_trader import PaperTrader
from src.execution.live_trader import LiveTrader
from src.execution.sizer import KellySizer
from src.kalshi.client import KalshiClient
from src.portfolio import Portfolio
from src.universe.discovery import KalshiMarketDiscovery
from src.universe.filter import (
    ExcludePrefixFilter, FilterChain, LiquidFilter,
    MaxSpreadFilter, MinVolumeFilter, SeriesWhitelistFilter,
)

logger = logging.getLogger(__name__)


def _configure_logging(level_str: str) -> None:
    level = getattr(logging, level_str.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/bot.log"),
        ],
    )


_running = True


def _shutdown(signum, frame):
    global _running
    logger.info("Shutdown signal received, stopping after current iteration...")
    _running = False


def _load_strategy(name: str):
    module = importlib.import_module(f"src.strategies.{name}")
    class_name = "".join(part.capitalize() for part in name.split("_"))
    return getattr(module, class_name)()


def _build_filter(cfg) -> FilterChain:
    filters = [
        LiquidFilter(),
        MinVolumeFilter(cfg.universe.min_volume),
        MaxSpreadFilter(cfg.universe.max_spread_cents),
        ExcludePrefixFilter(cfg.universe.exclude_prefixes),
    ]
    if cfg.universe.series_whitelist:
        filters.append(SeriesWhitelistFilter(cfg.universe.series_whitelist))
    return FilterChain(filters)


def main():
    import os
    os.makedirs("logs", exist_ok=True)

    config = load_config("config.yaml")
    _configure_logging(config.logging.level)
    logger.info(
        "Starting kalshi_bot | env=%s mode=%s strategy=%s",
        config.kalshi.environment, config.execution.mode, config.trading.strategy,
    )

    client = KalshiClient(
        base_url=config.kalshi.base_url,
        api_key_id=config.kalshi.api_key_id,
        private_key_path=config.kalshi.api_private_key_path,
    )
    discovery = KalshiMarketDiscovery(
        client=client,
        window_hours=config.universe.window_hours,
    )
    universe_filter = _build_filter(config)
    strategy = _load_strategy(config.trading.strategy)
    sizer = KellySizer(risk=config.risk)
    portfolio = Portfolio(balance_cents=config.execution.paper_starting_balance_cents)

    if config.execution.mode == "live":
        logger.warning("LIVE TRADING ENABLED — real orders will be placed on PRODUCTION")
        executor = LiveTrader(risk=config.risk, portfolio=portfolio, client=client)
    elif config.execution.mode == "demo":
        logger.info("Demo trading enabled — real orders will be placed on DEMO (fake money)")
        executor = LiveTrader(risk=config.risk, portfolio=portfolio, client=client)
    else:
        executor = PaperTrader(risk=config.risk, portfolio=portfolio)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    last_discovery = 0.0
    active_markets: dict = {}

    iteration = 0
    while _running:
        iteration += 1
        logger.info("--- iteration %d ---", iteration)

        try:
            # Refresh universe on first run and then every refresh_interval_seconds
            now = time.monotonic()
            if now - last_discovery >= config.universe.refresh_interval_seconds:
                logger.info("Refreshing market universe...")
                all_markets = discovery.refresh()
                filtered = universe_filter.apply(all_markets)
                active_markets = {m.ticker: m for m in filtered}
                last_discovery = now
                logger.info(
                    "Universe: %d discovered → %d after filters",
                    len(all_markets), len(active_markets),
                )

            if not active_markets:
                logger.warning("No active markets — check universe config")
            else:
                estimates = strategy.estimate_edge(active_markets)
                if estimates:
                    signals = sizer.size(estimates, active_markets, portfolio)
                    if signals:
                        executor.execute(signals, active_markets)

        except Exception as e:
            logger.error("Error in main loop: %s", e, exc_info=True)

        if _running:
            time.sleep(config.trading.loop_interval_seconds)

    if isinstance(executor, PaperTrader):
        executor.summary()
    logger.info("kalshi_bot stopped.")


if __name__ == "__main__":
    main()
