import pytest
from src.config import RiskConfig
from src.execution.paper_trader import PaperTrader
from src.kalshi.models import Market, OrderAction, Side, Signal


def make_risk(**kwargs) -> RiskConfig:
    defaults = dict(max_contracts_per_market=10, max_open_orders=5, max_daily_loss_cents=5000)
    return RiskConfig(**{**defaults, **kwargs})


def make_signal(ticker="TEST", quantity=5, **kwargs) -> Signal:
    return Signal(ticker=ticker, side=Side.YES, action=OrderAction.BUY,
                  quantity=quantity, limit_price=50, **kwargs)


def make_market(ticker="TEST") -> Market:
    return Market(ticker=ticker, title="Test", status="open",
                  yes_bid=45, yes_ask=55, volume=100, open_interest=50)


def test_quantity_capped_to_max_contracts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_contracts_per_market=3))
    signal = make_signal(quantity=10)
    approved = trader._apply_risk([signal], {})
    assert approved[0].quantity == 3


def test_quantity_within_limit_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_contracts_per_market=10))
    signal = make_signal(quantity=7)
    approved = trader._apply_risk([signal], {})
    assert approved[0].quantity == 7


def test_signals_blocked_when_daily_loss_exceeded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_daily_loss_cents=100))
    trader.record_loss(100)
    signals = [make_signal()]
    approved = trader._apply_risk(signals, {})
    assert approved == []


def test_signals_pass_when_just_under_daily_loss_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_daily_loss_cents=100))
    trader.record_loss(99)
    signals = [make_signal()]
    approved = trader._apply_risk(signals, {})
    assert len(approved) == 1


def test_daily_loss_resets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk(max_daily_loss_cents=100))
    trader.record_loss(200)
    trader.reset_daily_loss()
    signals = [make_signal()]
    approved = trader._apply_risk(signals, {})
    assert len(approved) == 1
