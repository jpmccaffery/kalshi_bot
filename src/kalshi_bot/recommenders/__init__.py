"""
Versioned recommenders for live trading and historical backtesting.

Live trading:
    from kalshi_bot.temp_recommender import TemperatureRecommender

Backtesting against historical parquet data:
    from kalshi_bot.recommenders.v1_historical import TemperatureRecommenderV1Historical
"""
from .v1 import TemperatureRecommenderV1
from .v1_historical import TemperatureRecommenderV1Historical

__all__ = ["TemperatureRecommenderV1", "TemperatureRecommenderV1Historical"]
