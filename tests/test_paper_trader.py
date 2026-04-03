import pytest
from src.config import RiskConfig
from src.execution.paper_trader import PaperTrader
from src.kalshi.models import Market, OrderAction, Side, Signal


def make_risk() -> RiskConfig:
    return RiskConfig(max_contracts_per_market=10, max_open_orders=5, max_daily_loss_cents=100_000)


def make_market(ticker="TEST", yes_bid=40, yes_ask=60) -> Market:
    return Market(ticker=ticker, title="Test", status="open",
                  yes_bid=yes_bid, yes_ask=yes_ask, volume=100, open_interest=50)


def make_signal(side=Side.YES, action=OrderAction.BUY, quantity=1, limit_price=None) -> Signal:
    return Signal(ticker="TEST", side=side, action=action,
                  quantity=quantity, limit_price=limit_price)


@pytest.fixture
def trader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return PaperTrader(risk=make_risk(), starting_balance_cents=10_000)


# ----------------------------------------------------------------- fill price

def test_fill_price_uses_limit_price(trader):
    signal = make_signal(side=Side.YES, limit_price=45)
    market = make_market(yes_bid=40, yes_ask=60)
    assert trader._fill_price(signal, market) == 45


def test_fill_price_yes_market_order_uses_mid(trader):
    signal = make_signal(side=Side.YES, limit_price=None)
    market = make_market(yes_bid=40, yes_ask=60)
    assert trader._fill_price(signal, market) == 50  # mid of 40+60


def test_fill_price_no_market_order_uses_no_mid(trader):
    signal = make_signal(side=Side.NO, limit_price=None)
    market = make_market(yes_bid=40, yes_ask=60)
    assert trader._fill_price(signal, market) == 50  # no_mid = 100 - yes_mid


# ----------------------------------------------------------------- balance

def test_buy_decreases_balance(trader):
    signal = make_signal(action=OrderAction.BUY, quantity=2, limit_price=50)
    markets = {"TEST": make_market()}
    trader._execute([signal], markets)
    assert trader.balance_cents == 10_000 - (50 * 2)


def test_sell_increases_balance(trader):
    signal = make_signal(action=OrderAction.SELL, quantity=2, limit_price=50)
    markets = {"TEST": make_market()}
    trader._execute([signal], markets)
    assert trader.balance_cents == 10_000 + (50 * 2)


# ----------------------------------------------------------------- positions

def test_buy_yes_increases_yes_quantity(trader):
    signal = make_signal(side=Side.YES, action=OrderAction.BUY, quantity=3, limit_price=50)
    trader._execute([signal], {"TEST": make_market()})
    assert trader.positions["TEST"].yes_quantity == 3


def test_buy_no_increases_no_quantity(trader):
    signal = make_signal(side=Side.NO, action=OrderAction.BUY, quantity=2, limit_price=50)
    trader._execute([signal], {"TEST": make_market()})
    assert trader.positions["TEST"].no_quantity == 2


def test_sell_yes_decreases_yes_quantity(trader):
    # Buy first, then sell
    buy = make_signal(side=Side.YES, action=OrderAction.BUY, quantity=5, limit_price=50)
    sell = make_signal(side=Side.YES, action=OrderAction.SELL, quantity=3, limit_price=60)
    markets = {"TEST": make_market()}
    trader._execute([buy], markets)
    trader._execute([sell], markets)
    assert trader.positions["TEST"].yes_quantity == 2


def test_missing_market_data_skips_signal(trader):
    signal = make_signal()
    fills = trader._execute([signal], {})
    assert fills == []
    assert trader.balance_cents == 10_000  # unchanged


# ----------------------------------------------------------------- csv logging

def test_fills_csv_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk())
    assert (tmp_path / "logs" / "paper_fills.csv").exists()


def test_fills_csv_row_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trader = PaperTrader(risk=make_risk())
    signal = make_signal(limit_price=50)
    trader._execute([signal], {"TEST": make_market()})
    rows = (tmp_path / "logs" / "paper_fills.csv").read_text().strip().splitlines()
    assert len(rows) == 2  # header + 1 fill
