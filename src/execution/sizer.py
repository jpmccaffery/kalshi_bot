"""
KellySizer — translates EdgeEstimates into sized Signals.

Kelly Criterion for a binary contract priced at c cents (0-100):
  f* = (p - c/100) / (1 - c/100)

where p is your estimated probability. This gives the fraction of bankroll
to allocate. Applied symmetrically to both YES and NO sides.

Multiplying by confidence gives fractional Kelly, which reduces variance at
the cost of some expected return. The default EdgeEstimate confidence of 0.5
means half-Kelly, which is a common practical choice.

The sizer also handles position rebalancing: if you hold the opposite side
from what Kelly recommends, it closes that position first.
"""

import logging
import math
from dataclasses import dataclass

from src.config import RiskConfig
from src.kalshi.models import EdgeEstimate, Market, OrderAction, Side, Signal
from src.portfolio import Portfolio

logger = logging.getLogger(__name__)

MIN_KELLY = 0.01   # ignore edges below 1% — not worth the spread cost


def kelly_fraction(prob: float, price_cents: int) -> float:
    """
    Kelly fraction for buying a contract at price_cents with win probability prob.

    Returns 0.0 if there is no positive edge.
    """
    c = price_cents / 100
    if c <= 0 or c >= 1:
        return 0.0
    return max(0.0, (prob - c) / (1 - c))


class KellySizer:
    def __init__(self, risk: RiskConfig):
        self.risk = risk

    def size(
        self,
        estimates: list[EdgeEstimate],
        markets: dict[str, Market],
        portfolio: Portfolio,
    ) -> list[Signal]:
        signals = []
        for estimate in estimates:
            market = markets.get(estimate.ticker)
            if market is None:
                logger.warning("No market data for %s — skipping estimate", estimate.ticker)
                continue
            if market.status != "open":
                continue
            signals.extend(self._size_market(estimate, market, portfolio))
        return signals

    def _size_market(
        self,
        estimate: EdgeEstimate,
        market: Market,
        portfolio: Portfolio,
    ) -> list[Signal]:
        signals = []

        f_yes = kelly_fraction(estimate.yes_probability, market.yes_ask) * estimate.confidence
        f_no  = kelly_fraction(1 - estimate.yes_probability, market.no_ask) * estimate.confidence

        current_yes = portfolio.yes_quantity(estimate.ticker)
        current_no  = portfolio.no_quantity(estimate.ticker)

        if f_yes >= MIN_KELLY:
            # Close any opposing NO position
            if current_no > 0:
                signals.append(Signal(
                    ticker=estimate.ticker,
                    side=Side.NO,
                    action=OrderAction.SELL,
                    quantity=current_no,
                    limit_price=market.no_bid,
                    reason="closing NO before opening YES",
                ))
            # Buy YES up to Kelly-optimal quantity
            target = self._contracts(f_yes, portfolio.balance_cents, market.yes_ask)
            delta = target - current_yes
            if delta > 0:
                signals.append(Signal(
                    ticker=estimate.ticker,
                    side=Side.YES,
                    action=OrderAction.BUY,
                    quantity=delta,
                    limit_price=market.yes_ask,
                    kelly_fraction=f_yes,
                    reason=f"p={estimate.yes_probability:.3f} f={f_yes:.3f}",
                ))

        elif f_no >= MIN_KELLY:
            # Close any opposing YES position
            if current_yes > 0:
                signals.append(Signal(
                    ticker=estimate.ticker,
                    side=Side.YES,
                    action=OrderAction.SELL,
                    quantity=current_yes,
                    limit_price=market.yes_bid,
                    reason="closing YES before opening NO",
                ))
            # Buy NO up to Kelly-optimal quantity
            target = self._contracts(f_no, portfolio.balance_cents, market.no_ask)
            delta = target - current_no
            if delta > 0:
                signals.append(Signal(
                    ticker=estimate.ticker,
                    side=Side.NO,
                    action=OrderAction.BUY,
                    quantity=delta,
                    limit_price=market.no_ask,
                    kelly_fraction=f_no,
                    reason=f"p_no={1 - estimate.yes_probability:.3f} f={f_no:.3f}",
                ))

        else:
            logger.debug(
                "[%s] no edge (p=%.3f yes_ask=%dc no_ask=%dc)",
                estimate.ticker, estimate.yes_probability, market.yes_ask, market.no_ask,
            )

        return signals

    def _contracts(self, kelly: float, balance_cents: int, price_cents: int) -> int:
        if price_cents <= 0:
            return 0
        raw = math.floor(kelly * balance_cents / price_cents)
        return min(raw, self.risk.max_contracts_per_market)
