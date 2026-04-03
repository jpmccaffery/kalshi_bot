import pytest
from src.kalshi.models import Market


def make_market(**kwargs) -> Market:
    defaults = dict(ticker="TEST", title="Test Market", status="open",
                    yes_bid=40, yes_ask=60, volume=100, open_interest=50)
    return Market(**{**defaults, **kwargs})


def test_no_bid_is_complement_of_yes_ask():
    m = make_market(yes_ask=60)
    assert m.no_bid == 40


def test_no_ask_is_complement_of_yes_bid():
    m = make_market(yes_bid=40)
    assert m.no_ask == 60


def test_yes_mid():
    m = make_market(yes_bid=40, yes_ask=60)
    assert m.yes_mid == 50.0


def test_no_mid():
    m = make_market(yes_bid=40, yes_ask=60)
    assert m.no_mid == 50.0


def test_yes_mid_asymmetric():
    m = make_market(yes_bid=30, yes_ask=50)
    assert m.yes_mid == 40.0
    assert m.no_mid == 60.0


def test_no_bid_no_ask_sum_to_100():
    m = make_market(yes_bid=35, yes_ask=45)
    assert m.yes_ask + m.no_bid == 100
    assert m.yes_bid + m.no_ask == 100
