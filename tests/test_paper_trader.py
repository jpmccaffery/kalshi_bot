import pytest
from src.config import RiskConfig
from src.execution.paper_trader import PaperTrader
from src.kalshi.models import Market, OrderAction, Side, Signal
from src.portfolio import Portfolio


def make_risk() -> RiskConfig:
    return RiskConfig(max_contracts_per_market=100, max_open_orders=5, max_daily_loss_cents=100_000)


def make_portfolio(balance=10_000) -> Portfolio:
    return Portfolio(balance_cents=balance)


def make_market(ticker="TEST", yes_bid=40, yes_ask=60) -> Market:
    return Market(ticker=ticker, title="Test", status="open",
                  yes_bid=yes_bid, yes_ask=yes_ask, volume=100, open_interest=50)


def make_signal(side=Side.YES, action=OrderAction.BUY, quantity=1, limit_price=None) -> Signal:
    return Signal(ticker="TEST", side=side, action=action,
                  quantity=quantity, limit_price=limit_price)


@pytest.fixture
def trader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return PaperTrader(risk=make_risk(), portfolio=make_portfolio())


# ----------------------------------------------------------------- fill price

def test_fill_price_uses_limit_price(trader):
    signal = make_signal(side=Side.YES, limit_price=45)
    assert trader._fill_price(signal, make_market()) == 45


def test_fill_price_yes_market_order_uses_mid(trader):
    assert trader._fill_price(make_signal(limit_price=None), make_market(yes_bid=40, yes_ask=60)) == 50


def test_fill_price_no_market_order_uses_no_mid(trader):
    signal = make_signal(side=Side.NO, limit_price=None)
    assert trader._fill_price(signal, make_market(yes_bid=40, yes_ask=60)) == 50


# ----------------------------------------------------------------- portfolio delegation

def test_buy_reduces_portfolio_balance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    portfolio = make_portfolio(balance=10_000)
    trader = PaperTrader(risk=make_risk(), portfolio=portfolio)
    trader.execute([make_signal(action=OrderAction.BUY, quantity=2, limit_price=50)],
                   {"TEST": make_market()})
    assert portfolio.balance_cents == 10_000 - 100


def test_buy_updates_portfolio_position(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    portfolio = make_portfolio()
    trader = PaperTrader(risk=make_risk(), portfolio=portfolio)
    trader.execute([make_signal(side=Side.YES, action=OrderAction.BUY, quantity=3, limit_price=50)],
                   {"TEST": make_market()})
    assert portfolio.yes_quantity("TEST") == 3


# ----------------------------------------------------------------- missing market data

def test_missing_market_data_skips_signal(trader):
    fills = trader._execute([make_signal()], {})
    assert fills == []


# ----------------------------------------------------------------- csv logging

def test_fills_csv_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    PaperTrader(risk=make_risk(), portfolio=make_portfolio())
    assert (tmp_path / "logs" / "paper_fills.csv").exists()


def test_fills_csv_row_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(), portfolio=make_portfolio())
    trader._execute([make_signal(limit_price=50)], {"TEST": make_market()})
    rows = (tmp_path / "logs" / "paper_fills.csv").read_text().strip().splitlines()
    assert len(rows) == 2  # header + 1 fill
