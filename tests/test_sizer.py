import pytest
from src.config import RiskConfig
from src.execution.sizer import KellySizer, kelly_fraction
from src.kalshi.models import EdgeEstimate, Market, OrderAction, Side
from src.portfolio import Portfolio


def make_risk(**kwargs) -> RiskConfig:
    defaults = dict(max_contracts_per_market=100, max_open_orders=5, max_daily_loss_cents=100_000)
    return RiskConfig(**{**defaults, **kwargs})


def make_market(ticker="TEST", yes_bid=45, yes_ask=55, status="open") -> Market:
    return Market(ticker=ticker, title="Test", status=status,
                  yes_bid=yes_bid, yes_ask=yes_ask, volume=100, open_interest=50)


def make_portfolio(balance=100_000) -> Portfolio:
    return Portfolio(balance_cents=balance)


# ----------------------------------------------------------------- kelly_fraction formula

def test_kelly_zero_when_no_edge():
    # p == market price → no edge
    assert kelly_fraction(0.55, 55) == 0.0


def test_kelly_zero_when_negative_edge():
    # p < market price → negative edge, should clamp to 0
    assert kelly_fraction(0.40, 55) == 0.0


def test_kelly_positive_with_edge():
    # p=0.70, price=55c → f = (0.70 - 0.55) / (1 - 0.55) = 0.15/0.45 ≈ 0.333
    f = kelly_fraction(0.70, 55)
    assert abs(f - (0.15 / 0.45)) < 1e-9


def test_kelly_full_edge():
    # Certain win (p=1.0), price=50c → f = (1.0 - 0.5) / 0.5 = 1.0
    assert kelly_fraction(1.0, 50) == 1.0


def test_kelly_returns_zero_for_zero_price():
    assert kelly_fraction(0.9, 0) == 0.0


def test_kelly_returns_zero_for_100_price():
    assert kelly_fraction(0.9, 100) == 0.0


# ----------------------------------------------------------------- sizer signal generation

def test_no_signals_when_no_edge():
    sizer = KellySizer(make_risk())
    estimates = [EdgeEstimate(ticker="TEST", yes_probability=0.55)]
    markets = {"TEST": make_market(yes_ask=55)}  # market already priced at 55c
    signals = sizer.size(estimates, markets, make_portfolio())
    assert signals == []


def test_buy_yes_when_yes_edge():
    sizer = KellySizer(make_risk())
    estimates = [EdgeEstimate(ticker="TEST", yes_probability=0.80, confidence=1.0)]
    markets = {"TEST": make_market(yes_bid=45, yes_ask=55)}
    portfolio = make_portfolio(balance=10_000)
    signals = sizer.size(estimates, markets, portfolio)
    buys = [s for s in signals if s.side == Side.YES and s.action == OrderAction.BUY]
    assert len(buys) == 1
    assert buys[0].quantity > 0


def test_buy_no_when_no_edge():
    sizer = KellySizer(make_risk())
    # p=0.20 means NO has 80% probability; no_ask = 100 - yes_bid = 55
    estimates = [EdgeEstimate(ticker="TEST", yes_probability=0.20, confidence=1.0)]
    markets = {"TEST": make_market(yes_bid=45, yes_ask=55)}
    portfolio = make_portfolio(balance=10_000)
    signals = sizer.size(estimates, markets, portfolio)
    buys = [s for s in signals if s.side == Side.NO and s.action == OrderAction.BUY]
    assert len(buys) == 1
    assert buys[0].quantity > 0


def test_confidence_scales_quantity():
    sizer = KellySizer(make_risk())
    market = make_market(yes_bid=45, yes_ask=55)
    # Small balance so quantities stay well under max_contracts_per_market cap:
    # f=0.556, balance=1000, price=55 → qty_full=10, qty_half=5
    portfolio = make_portfolio(balance=1_000)

    signals_full = sizer.size(
        [EdgeEstimate("TEST", yes_probability=0.80, confidence=1.0)],
        {"TEST": market}, portfolio,
    )
    signals_half = sizer.size(
        [EdgeEstimate("TEST", yes_probability=0.80, confidence=0.5)],
        {"TEST": market}, portfolio,
    )
    qty_full = next(s.quantity for s in signals_full if s.action == OrderAction.BUY)
    qty_half = next(s.quantity for s in signals_half if s.action == OrderAction.BUY)
    assert qty_full == qty_half * 2


def test_quantity_capped_by_max_contracts():
    sizer = KellySizer(make_risk(max_contracts_per_market=5))
    estimates = [EdgeEstimate("TEST", yes_probability=0.99, confidence=1.0)]
    markets = {"TEST": make_market(yes_bid=1, yes_ask=2)}  # very cheap → huge Kelly qty
    portfolio = make_portfolio(balance=1_000_000)
    signals = sizer.size(estimates, markets, portfolio)
    buys = [s for s in signals if s.action == OrderAction.BUY]
    assert buys[0].quantity <= 5


def test_no_signals_for_closed_market():
    sizer = KellySizer(make_risk())
    estimates = [EdgeEstimate("TEST", yes_probability=0.90, confidence=1.0)]
    markets = {"TEST": make_market(status="closed")}
    signals = sizer.size(estimates, markets, make_portfolio())
    assert signals == []


def test_closes_opposing_no_position_before_buying_yes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.kalshi.models import Fill
    sizer = KellySizer(make_risk())
    portfolio = make_portfolio(balance=10_000)
    # Manually put a NO position on the portfolio
    portfolio.apply_fill(Fill("TEST", Side.NO, OrderAction.BUY, 3, 50, ""))

    estimates = [EdgeEstimate("TEST", yes_probability=0.85, confidence=1.0)]
    markets = {"TEST": make_market(yes_bid=45, yes_ask=55)}
    signals = sizer.size(estimates, markets, portfolio)

    # First signal should close the NO position
    assert signals[0].side == Side.NO
    assert signals[0].action == OrderAction.SELL
    assert signals[0].quantity == 3


def test_no_signals_when_no_market_data():
    sizer = KellySizer(make_risk())
    estimates = [EdgeEstimate("MISSING", yes_probability=0.80)]
    signals = sizer.size(estimates, {}, make_portfolio())
    assert signals == []
