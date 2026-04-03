from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


@dataclass
class Market:
    ticker: str
    title: str
    status: str                        # open | closed | settled
    yes_bid: int                       # cents (0–99)
    yes_ask: int
    volume: int
    open_interest: int
    close_time: Optional[str] = None

    @property
    def no_bid(self) -> int:
        return 100 - self.yes_ask

    @property
    def no_ask(self) -> int:
        return 100 - self.yes_bid

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def no_mid(self) -> float:
        return 100 - self.yes_mid


@dataclass
class OrderBookLevel:
    price: int   # cents
    quantity: int


@dataclass
class OrderBook:
    ticker: str
    yes_bids: list[OrderBookLevel] = field(default_factory=list)
    yes_asks: list[OrderBookLevel] = field(default_factory=list)


@dataclass
class Signal:
    """A strategy's instruction to buy or sell a contract."""
    ticker: str
    side: Side
    action: OrderAction
    quantity: int
    limit_price: Optional[int] = None  # cents; None = market order
    reason: str = ""


@dataclass
class Fill:
    """A confirmed (or simulated) execution."""
    ticker: str
    side: Side
    action: OrderAction
    quantity: int
    price: int    # cents
    timestamp: str


@dataclass
class Position:
    ticker: str
    yes_quantity: int = 0
    no_quantity: int = 0
    realized_pnl_cents: int = 0
