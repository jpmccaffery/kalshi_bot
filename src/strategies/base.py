from abc import ABC, abstractmethod

from src.kalshi.models import Market, Signal


class Strategy(ABC):
    """
    Implement this interface to create a trading strategy.

    `generate_signals` is called each loop iteration with the latest market
    snapshot. Return a (possibly empty) list of Signals. The executor handles
    risk checks and order placement — the strategy only decides *what* to do.
    """

    @abstractmethod
    def generate_signals(self, markets: dict[str, Market]) -> list[Signal]:
        ...
