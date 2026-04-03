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
class EdgeEstimate:
    """
    A strategy's output: the estimated true probability that a market resolves YES.
    The sizer uses this — along with portfolio state and current prices — to compute
    Kelly-optimal position sizes.

    confidence: scales the Kelly fraction (fractional Kelly). 1.0 = full Kelly,
    0.5 = half Kelly. Use lower values when your probability estimate is uncertain.
    """
    ticker: str
    yes_probability: float    # 0.0–1.0
    confidence: float = 0.5   # default to half-Kelly for safety


@dataclass
class Signal:
    """A sized order instruction produced by the KellySizer."""
    ticker: str
    side: Side
    action: OrderAction
    quantity: int
    limit_price: Optional[int] = None   # cents; None = market order
    kelly_fraction: float = 0.0         # for audit logging
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
    yes_avg_cost: float = 0.0       # cents per contract
    no_quantity: int = 0
    no_avg_cost: float = 0.0        # cents per contract
    realized_pnl_cents: int = 0
