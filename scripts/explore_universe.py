"""
Explore the market universe using the discovery + filter pipeline.
Prints a summary of what's available after each filter stage.

Run with: docker exec kalshi_bot python scripts/explore_universe.py
"""
import sys
sys.path.insert(0, "/app")

from src.config import load_config
from src.kalshi.client import KalshiClient
from src.kalshi.models import Market
from src.universe.discovery import KalshiMarketDiscovery
from src.universe.filter import (
    ExcludePrefixFilter, FilterChain, LiquidFilter,
    MaxSpreadFilter, MinVolumeFilter, SeriesWhitelistFilter,
)

cfg = load_config("/app/config.yaml")
print(f"Environment: {cfg.kalshi.environment} ({cfg.kalshi.base_url})\n")

client = KalshiClient(
    base_url=cfg.kalshi.base_url,
    api_key_id=cfg.kalshi.api_key_id,
    private_key_path=cfg.kalshi.api_private_key_path,
)


def print_markets(markets: list[Market], limit: int = 20) -> None:
    print(f"\n  {'Ticker':<50} {'Bid':>4} {'Ask':>4} {'Sprd':>5} {'Vol24h':>8} {'Vol':>8}")
    print(f"  {'-'*82}")
    for m in sorted(markets, key=lambda x: -x.volume)[:limit]:
        spread = m.yes_ask - m.yes_bid
        print(f"  {m.ticker:<50} {m.yes_bid:>4} {m.yes_ask:>4} {spread:>5} {m.volume_24h:>8} {m.volume:>8}")
        print(f"    {m.title}")
    if len(markets) > limit:
        print(f"\n  ... and {len(markets) - limit} more")


def summarise(label: str, markets: list[Market]) -> None:
    total_vol = sum(m.volume for m in markets)
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Count: {len(markets)}   Total volume: {total_vol:,}")
    print(f"{'='*60}")
    print_markets(markets)


# Discovery
print(f"Running discovery (window={cfg.universe.window_hours}h)...")
discovery = KalshiMarketDiscovery(client, window_hours=cfg.universe.window_hours)
all_markets = discovery.refresh()
summarise("Raw discovery", all_markets)

# Show effect of each filter individually
stages = [
    ("Exclude prefixes",  ExcludePrefixFilter(cfg.universe.exclude_prefixes)),
    ("Liquid",            LiquidFilter()),
    ("Min volume",        MinVolumeFilter(cfg.universe.min_volume)),
    ("Max spread",        MaxSpreadFilter(cfg.universe.max_spread_cents)),
]
if cfg.universe.series_whitelist:
    stages.append(("Series whitelist", SeriesWhitelistFilter(cfg.universe.series_whitelist)))

markets = all_markets
for label, f in stages:
    markets = f.apply(markets)
    summarise(label, markets)
