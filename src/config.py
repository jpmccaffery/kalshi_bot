import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class KalshiConfig:
    environment: str = "demo"
    api_key_id: str = ""
    api_private_key_path: str = ""

    BASE_URLS = {
        "demo": "https://demo-api.kalshi.co/trade-api/v2",
        "production": "https://trading-api.kalshi.com/trade-api/v2",
    }

    @property
    def base_url(self) -> str:
        return self.BASE_URLS[self.environment]


@dataclass
class RiskConfig:
    max_contracts_per_market: int = 10
    max_open_orders: int = 5
    max_daily_loss_cents: int = 5000


@dataclass
class TradingConfig:
    strategy: str = "null_strategy"
    markets: list[str] = field(default_factory=list)
    loop_interval_seconds: int = 60


@dataclass
class ExecutionConfig:
    mode: str = "paper"
    paper_starting_balance_cents: int = 100_000


@dataclass
class Config:
    kalshi: KalshiConfig
    trading: TradingConfig
    risk: RiskConfig
    execution: ExecutionConfig


def load_config(path: str = "config.yaml") -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    kalshi_raw = raw.get("kalshi", {})
    kalshi = KalshiConfig(
        environment=kalshi_raw.get("environment", "demo"),
        api_key_id=os.environ.get("KALSHI_API_KEY_ID", ""),
        api_private_key_path=os.environ.get("KALSHI_API_PRIVATE_KEY_PATH", ""),
    )

    trading_raw = raw.get("trading", {})
    trading = TradingConfig(
        strategy=trading_raw.get("strategy", "null_strategy"),
        markets=trading_raw.get("markets", []),
        loop_interval_seconds=trading_raw.get("loop_interval_seconds", 60),
    )

    risk_raw = raw.get("risk", {})
    risk = RiskConfig(
        max_contracts_per_market=risk_raw.get("max_contracts_per_market", 10),
        max_open_orders=risk_raw.get("max_open_orders", 5),
        max_daily_loss_cents=risk_raw.get("max_daily_loss_cents", 5000),
    )

    exec_raw = raw.get("execution", {})
    execution = ExecutionConfig(
        mode=exec_raw.get("mode", "paper"),
        paper_starting_balance_cents=exec_raw.get("paper_starting_balance_cents", 100_000),
    )

    return Config(kalshi=kalshi, trading=trading, risk=risk, execution=execution)
