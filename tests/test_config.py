import os
import textwrap
import pytest
from unittest.mock import patch

from src.config import load_config


MINIMAL_YAML = textwrap.dedent("""\
    kalshi:
      environment: demo
    trading:
      strategy: null_strategy
      markets:
        - KXTEST-25DEC31
      loop_interval_seconds: 30
    risk:
      max_contracts_per_market: 5
      max_open_orders: 3
      max_daily_loss_cents: 2000
    execution:
      mode: paper
      paper_starting_balance_cents: 50000
""")


@pytest.fixture
def config_file(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_YAML)
    return str(p)


def test_loads_environment(config_file):
    cfg = load_config(config_file)
    assert cfg.kalshi.environment == "demo"


def test_base_url_demo(config_file):
    cfg = load_config(config_file)
    assert "demo" in cfg.kalshi.base_url


def test_trading_fields(config_file):
    cfg = load_config(config_file)
    assert cfg.trading.strategy == "null_strategy"
    assert cfg.trading.markets == ["KXTEST-25DEC31"]
    assert cfg.trading.loop_interval_seconds == 30


def test_risk_fields(config_file):
    cfg = load_config(config_file)
    assert cfg.risk.max_contracts_per_market == 5
    assert cfg.risk.max_daily_loss_cents == 2000


def test_execution_fields(config_file):
    cfg = load_config(config_file)
    assert cfg.execution.mode == "paper"
    assert cfg.execution.paper_starting_balance_cents == 50_000


def test_api_key_id_from_env(config_file):
    with patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test-key-123"}):
        cfg = load_config(config_file)
    assert cfg.kalshi.api_key_id == "test-key-123"


def test_production_base_url(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_YAML.replace("environment: demo", "environment: production"))
    cfg = load_config(str(p))
    assert "trading-api.kalshi.com" in cfg.kalshi.base_url
