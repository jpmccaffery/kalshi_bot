import logging
from abc import ABC, abstractmethod

from src.config import RiskConfig
from src.kalshi.models import Fill, Signal
from src.portfolio import Portfolio

logger = logging.getLogger(__name__)


class Executor(ABC):
    """
    Receives sized Signals from the KellySizer, applies a final risk check,
    executes them (paper or live), then updates the shared Portfolio.
    """

    def __init__(self, risk: RiskConfig, portfolio: Portfolio):
        self.risk = risk
        self.portfolio = portfolio
        self._daily_loss_cents: int = 0

    def execute(self, signals: list[Signal], markets: dict) -> list[Fill]:
        approved = self._apply_risk(signals)
        fills = self._execute(approved, markets)
        for fill in fills:
            pnl = self.portfolio.apply_fill(fill)
            if pnl < 0:
                self._daily_loss_cents += -pnl
        return fills

    def _apply_risk(self, signals: list[Signal]) -> list[Signal]:
        if self._daily_loss_cents >= self.risk.max_daily_loss_cents:
            logger.warning("Daily loss limit reached — blocking all signals")
            return []
        approved = []
        for signal in signals:
            if signal.quantity > self.risk.max_contracts_per_market:
                signal.quantity = self.risk.max_contracts_per_market
            approved.append(signal)
        return approved

    def reset_daily_loss(self) -> None:
        self._daily_loss_cents = 0

    @abstractmethod
    def _execute(self, signals: list[Signal], markets: dict) -> list[Fill]:
        ...
