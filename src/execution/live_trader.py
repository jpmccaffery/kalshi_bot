"""
Live trader — places real orders via the Kalshi API.

NOT ACTIVE: switches to this mode only when execution.mode = live in config.yaml.
Review your strategy thoroughly in paper mode before enabling.
"""

import logging

from src.config import RiskConfig
from src.execution.base import Executor
from src.kalshi.client import KalshiClient
from src.kalshi.models import Fill, Market, OrderAction, Side, Signal

logger = logging.getLogger(__name__)


class LiveTrader(Executor):
    def __init__(self, risk: RiskConfig, client: KalshiClient):
        super().__init__(risk)
        self._client = client

    def _execute(self, signals: list[Signal], markets: dict[str, Market]) -> list[Fill]:
        fills = []
        for signal in signals:
            try:
                result = self._client.place_order(
                    ticker=signal.ticker,
                    side=signal.side,
                    action=signal.action,
                    quantity=signal.quantity,
                    limit_price=signal.limit_price,
                )
                order = result.get("order", {})
                fill = Fill(
                    ticker=signal.ticker,
                    side=signal.side,
                    action=signal.action,
                    quantity=signal.quantity,
                    price=order.get("limit_price", signal.limit_price or 0),
                    timestamp=order.get("created_time", ""),
                )
                fills.append(fill)
                logger.info(
                    "[LIVE] %s %s %s x%d @ %dc | order_id=%s",
                    fill.action.value.upper(),
                    fill.side.value.upper(),
                    fill.ticker,
                    fill.quantity,
                    fill.price,
                    order.get("order_id", "?"),
                )
            except Exception as e:
                logger.error("Order failed for %s: %s", signal.ticker, e)
        return fills
