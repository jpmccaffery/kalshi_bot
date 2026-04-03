from src.kalshi.models import Market
from src.strategies.null_strategy import NullStrategy


def make_market(ticker="TEST", yes_bid=40, yes_ask=60) -> Market:
    return Market(ticker=ticker, title="Test", status="open",
                  yes_bid=yes_bid, yes_ask=yes_ask, volume=0, open_interest=0)


def test_returns_no_signals():
    strategy = NullStrategy()
    markets = {"TEST": make_market()}
    assert strategy.generate_signals(markets) == []


def test_handles_empty_markets():
    strategy = NullStrategy()
    assert strategy.generate_signals({}) == []


def test_handles_multiple_markets():
    strategy = NullStrategy()
    markets = {
        "TICKER-A": make_market("TICKER-A"),
        "TICKER-B": make_market("TICKER-B"),
    }
    assert strategy.generate_signals(markets) == []
