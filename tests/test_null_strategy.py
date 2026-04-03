from src.kalshi.models import Market
from src.strategies.null_strategy import NullStrategy


def make_market(ticker="TEST", yes_bid=40, yes_ask=60) -> Market:
    return Market(ticker=ticker, title="Test", status="open",
                  yes_bid=yes_bid, yes_ask=yes_ask, volume=0, open_interest=0)


def test_returns_no_estimates():
    strategy = NullStrategy()
    assert strategy.estimate_edge({"TEST": make_market()}) == []


def test_handles_empty_markets():
    assert NullStrategy().estimate_edge({}) == []


def test_handles_multiple_markets():
    markets = {"A": make_market("A"), "B": make_market("B")}
    assert NullStrategy().estimate_edge(markets) == []
