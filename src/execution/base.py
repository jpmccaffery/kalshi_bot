from abc import ABC, abstractmethod

from src.kalshi.models import Fill, Signal
from src.config import RiskConfig


class Executor(ABC):
    """
    Receives signals from the strategy, applies risk controls, and either
    simulates (paper) or places (live) orders.
    """

    def __init__(self, risk: RiskConfig):
        self.risk = risk
        self._daily_loss_cents: int = 0

    def execute(self, signals: list[Signal], markets: dict) -> list[Fill]:
        approved = self._apply_risk(signals, markets)
        return self._execute(approved, markets)

    def _apply_risk(self, signals: list[Signal], markets: dict) -> list[Signal]:
        approved = []
        for signal in signals:
            if signal.quantity > self.risk.max_contracts_per_market:
                signal.quantity = self.risk.max_contracts_per_market
            if self._daily_loss_cents >= self.risk.max_daily_loss_cents:
                break
            approved.append(signal)
        return approved

    def record_loss(self, cents: int) -> None:
        self._daily_loss_cents += cents

    def reset_daily_loss(self) -> None:
        self._daily_loss_cents = 0

    @abstractmethod
    def _execute(self, signals: list[Signal], markets: dict) -> list[Fill]:
        ...
