import importlib
import logging
import signal
import sys
import time

from src.config import load_config
from src.data.base import KalshiDataFeed
from src.execution.paper_trader import PaperTrader
from src.execution.live_trader import LiveTrader
from src.kalshi.client import KalshiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

_running = True


def _shutdown(signum, frame):
    global _running
    logger.info("Shutdown signal received, stopping after current iteration...")
    _running = False


def _load_strategy(name: str):
    """Dynamically load a strategy by module name from src/strategies/."""
    module = importlib.import_module(f"src.strategies.{name}")
    # Convention: class name is CamelCase of the module name
    class_name = "".join(part.capitalize() for part in name.split("_"))
    return getattr(module, class_name)()


def main():
    import os
    os.makedirs("logs", exist_ok=True)

    config = load_config("config.yaml")
    logger.info("Starting kalshi_bot | env=%s mode=%s strategy=%s",
                config.kalshi.environment, config.execution.mode, config.trading.strategy)

    client = KalshiClient(
        base_url=config.kalshi.base_url,
        api_key_id=config.kalshi.api_key_id,
        private_key_path=config.kalshi.api_private_key_path,
    )
    feed = KalshiDataFeed(client)
    strategy = _load_strategy(config.trading.strategy)

    if config.execution.mode == "live":
        logger.warning("LIVE TRADING ENABLED — real orders will be placed")
        executor = LiveTrader(risk=config.risk, client=client)
    else:
        executor = PaperTrader(
            risk=config.risk,
            starting_balance_cents=config.execution.paper_starting_balance_cents,
        )

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    iteration = 0
    while _running:
        iteration += 1
        logger.info("--- iteration %d ---", iteration)
        try:
            markets = feed.fetch(config.trading.markets)
            if not markets:
                logger.warning("No market data returned — check your tickers in config.yaml")
            else:
                signals = strategy.generate_signals(markets)
                if signals:
                    executor.execute(signals, markets)
        except Exception as e:
            logger.error("Error in main loop: %s", e, exc_info=True)

        if _running:
            time.sleep(config.trading.loop_interval_seconds)

    if isinstance(executor, PaperTrader):
        executor.summary()
    logger.info("kalshi_bot stopped.")


if __name__ == "__main__":
    main()
