from __future__ import annotations

import dataclasses
from decimal import Decimal

import pandas as pd

from trading_bot.models import Order


class PaddedSizer:
    """
    Wraps any position sizer and:
      1. Handles "short" (buy-NO) signals by substituting no_ask for yes_ask
         in the prices DataFrame so the base sizer prices them correctly, then
         tags the resulting orders with metadata={"kalshi_side": "no"}.
      2. Adds a flat cent-denominated padding to the limit price of every buy
         order so we clear the book more reliably.

    Configured via TRADING_LIMIT_PADDING_CENTS (default 1 cent).
    """

    def __init__(self, base_sizer, flat_padding: Decimal = Decimal("0.01")) -> None:
        self._base         = base_sizer
        self._flat_padding = flat_padding

    def size(
        self,
        signals: list,
        capital: Decimal,
        prices: pd.DataFrame,
    ) -> list[Order]:
        short_syms = {s.symbol for s in signals if s.direction == "short"}

        # For short signals, swap yes_ask ← no_ask so the base sizer prices
        # them against the NO ask rather than the YES ask.
        if short_syms and not prices.empty and "no_ask" in prices.columns:
            prices = prices.copy()
            mask = prices["symbol"].isin(short_syms)
            prices.loc[mask, "yes_ask"] = prices.loc[mask, "no_ask"]

        orders = self._base.size(signals, capital, prices)

        # Tag orders for short symbols with kalshi_side=no.
        orders = [
            dataclasses.replace(o, metadata={"kalshi_side": "no"})
            if o.symbol in short_syms else o
            for o in orders
        ]

        # Apply flat padding to all buy orders.
        if self._flat_padding:
            orders = [
                dataclasses.replace(o, limit_price=o.limit_price + self._flat_padding)
                if o.side == "buy" else o
                for o in orders
            ]

        return orders
