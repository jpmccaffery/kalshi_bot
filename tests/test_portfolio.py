import pytest
from src.kalshi.models import Fill, OrderAction, Side
from src.portfolio import Portfolio


def make_fill(action=OrderAction.BUY, side=Side.YES, quantity=1, price=50) -> Fill:
    return Fill(ticker="TEST", side=side, action=action,
                quantity=quantity, price=price, timestamp="2025-01-01T00:00:00+00:00")


# ----------------------------------------------------------------- balance

def test_buy_reduces_balance():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=2, price=50))
    assert p.balance_cents == 10_000 - 100


def test_sell_increases_balance():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=2, price=50))
    p.apply_fill(make_fill(action=OrderAction.SELL, quantity=2, price=60))
    assert p.balance_cents == 10_000 - 100 + 120


# ----------------------------------------------------------------- positions

def test_buy_yes_increments_quantity():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(side=Side.YES, action=OrderAction.BUY, quantity=3))
    assert p.yes_quantity("TEST") == 3


def test_buy_no_increments_quantity():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(side=Side.NO, action=OrderAction.BUY, quantity=2))
    assert p.no_quantity("TEST") == 2


def test_sell_yes_decrements_quantity():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=5, price=50))
    p.apply_fill(make_fill(action=OrderAction.SELL, quantity=3, price=60))
    assert p.yes_quantity("TEST") == 2


# ----------------------------------------------------------------- avg cost

def test_avg_cost_single_buy():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=4, price=50))
    assert p.positions["TEST"].yes_avg_cost == 50.0


def test_avg_cost_two_buys():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=2, price=40))
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=2, price=60))
    assert p.positions["TEST"].yes_avg_cost == 50.0


def test_avg_cost_resets_after_full_close():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=2, price=50))
    p.apply_fill(make_fill(action=OrderAction.SELL, quantity=2, price=60))
    assert p.positions["TEST"].yes_avg_cost == 0.0


# ----------------------------------------------------------------- realized P&L

def test_realized_pnl_on_profitable_close():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=4, price=50))
    pnl = p.apply_fill(make_fill(action=OrderAction.SELL, quantity=4, price=70))
    assert pnl == (70 - 50) * 4   # 80 cents profit
    assert p.positions["TEST"].realized_pnl_cents == 80


def test_realized_pnl_on_losing_close():
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=2, price=60))
    pnl = p.apply_fill(make_fill(action=OrderAction.SELL, quantity=2, price=40))
    assert pnl == (40 - 60) * 2   # -40 cents loss


def test_buy_returns_zero_pnl():
    p = Portfolio(balance_cents=10_000)
    pnl = p.apply_fill(make_fill(action=OrderAction.BUY))
    assert pnl == 0


# ----------------------------------------------------------------- total value

def test_total_value_no_positions():
    from src.kalshi.models import Market
    p = Portfolio(balance_cents=10_000)
    markets = {"TEST": Market("TEST", "T", "open", 40, 60, 0, 0)}
    assert p.total_value_cents(markets) == 10_000


def test_total_value_includes_open_position():
    from src.kalshi.models import Market
    p = Portfolio(balance_cents=10_000)
    p.apply_fill(make_fill(action=OrderAction.BUY, quantity=10, price=50))
    # balance = 9500, YES position bid = 40 → 10 * 40 = 400
    markets = {"TEST": Market("TEST", "T", "open", 40, 60, 0, 0)}
    assert p.total_value_cents(markets) == 9_500 + 400
