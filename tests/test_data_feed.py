from unittest.mock import MagicMock
from src.data.base import KalshiDataFeed
from src.kalshi.models import Market


def make_market(ticker: str) -> Market:
    return Market(ticker=ticker, title=f"Market {ticker}", status="open",
                  yes_bid=40, yes_ask=60, volume=0, open_interest=0)


def test_fetch_returns_dict_keyed_by_ticker():
    client = MagicMock()
    client.get_markets.return_value = [make_market("AAA"), make_market("BBB")]
    feed = KalshiDataFeed(client)

    result = feed.fetch(["AAA", "BBB"])

    assert set(result.keys()) == {"AAA", "BBB"}
    assert result["AAA"].ticker == "AAA"


def test_fetch_passes_tickers_to_client():
    client = MagicMock()
    client.get_markets.return_value = []
    feed = KalshiDataFeed(client)

    feed.fetch(["X", "Y", "Z"])

    client.get_markets.assert_called_once_with(["X", "Y", "Z"])


def test_fetch_empty_tickers():
    client = MagicMock()
    client.get_markets.return_value = []
    feed = KalshiDataFeed(client)

    result = feed.fetch([])

    assert result == {}
