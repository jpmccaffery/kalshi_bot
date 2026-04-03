from abc import ABC, abstractmethod

from src.kalshi.models import EdgeEstimate, Market


class Strategy(ABC):
    """
    Implement this interface to create a trading strategy.

    `estimate_edge` is called each loop iteration with the latest market
    snapshot. Return a (possibly empty) list of EdgeEstimates — one per market
    you have a view on. Markets you omit will not be traded.

    The strategy's only job is to estimate probabilities. Position sizing,
    capital allocation, and risk controls are all handled downstream by the
    KellySizer and Executor.
    """

    @abstractmethod
    def estimate_edge(self, markets: dict[str, Market]) -> list[EdgeEstimate]:
        ...
