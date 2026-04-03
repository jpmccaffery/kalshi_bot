import pytest
from src.config import RiskConfig
from src.execution.paper_trader import PaperTrader
from src.kalshi.models import Market, OrderAction, Side, Signal
from src.portfolio import Portfolio


def make_risk(**kwargs) -> RiskConfig:
    defaults = dict(max_contracts_per_market=10, max_open_orders=5, max_daily_loss_cents=5000)
    return RiskConfig(**{**defaults, **kwargs})


def make_portfolio() -> Portfolio:
    return Portfolio(balance_cents=100_000)


def make_signal(ticker="TEST", quantity=5) -> Signal:
    return Signal(ticker=ticker, side=Side.YES, action=OrderAction.BUY,
                  quantity=quantity, limit_price=50)


@pytest.fixture
def trader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return PaperTrader(risk=make_risk(), portfolio=make_portfolio())


def test_quantity_capped_to_max_contracts(trader):
    signal = make_signal(quantity=20)
    approved = trader._apply_risk([signal])
    assert approved[0].quantity == 10


def test_quantity_within_limit_unchanged(trader):
    signal = make_signal(quantity=7)
    approved = trader._apply_risk([signal])
    assert approved[0].quantity == 7


def test_signals_blocked_when_daily_loss_exceeded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_daily_loss_cents=100), portfolio=make_portfolio())
    trader._daily_loss_cents = 100
    assert trader._apply_risk([make_signal()]) == []


def test_signals_pass_when_just_under_daily_loss_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_daily_loss_cents=100), portfolio=make_portfolio())
    trader._daily_loss_cents = 99
    assert len(trader._apply_risk([make_signal()])) == 1


def test_daily_loss_resets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_daily_loss_cents=100), portfolio=make_portfolio())
    trader._daily_loss_cents = 200
    trader.reset_daily_loss()
    assert len(trader._apply_risk([make_signal()])) == 1
