from __future__ import annotations

import dataclasses
from decimal import Decimal

import pandas as pd

from trading_bot.models import Order


class PaddedSizer:
    """
    Wraps any position sizer and adds a flat cent-denominated padding to the
    limit price of every buy order. Sell orders are left unchanged.

    The padding is on top of whatever limit_offset_pct the base sizer applies,
    so total limit = yes_ask * (1 + limit_offset_pct) + flat_padding.

    Configured via TRADING_LIMIT_PADDING_CENTS (default 1 cent).
    """

    def __init__(self, base_sizer, flat_padding: Decimal = Decimal("0.01")) -> None:
        self._base        = base_sizer
        self._flat_padding = flat_padding

    def size(
        self,
        signals: list,
        capital: Decimal,
        prices: pd.DataFrame,
    ) -> list[Order]:
        orders = self._base.size(signals, capital, prices)
        if not self._flat_padding:
            return orders
        return [
            dataclasses.replace(o, limit_price=o.limit_price + self._flat_padding)
            if o.side == "buy" else o
            for o in orders
        ]
