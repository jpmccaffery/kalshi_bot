from unittest.mock import MagicMock, patch
from src.kalshi.models import Market
from src.strategies.weather_strategy import WeatherStrategy, _prob_above, FORECAST_STD_F


def make_market(ticker="KXTEMPNYCH-26APR0319-T65.99") -> Market:
    return Market(ticker=ticker, title="NYC Temp", status="open",
                  yes_bid=40, yes_ask=60, volume=100, open_interest=50)


# ----------------------------------------------------------------- probability model

def test_prob_above_when_forecast_equals_threshold():
    # Forecast right at threshold → 50% chance of being above
    assert abs(_prob_above(65.99, 65.99) - 0.5) < 1e-6


def test_prob_above_when_forecast_well_above_threshold():
    # Forecast 10°F above threshold with σ=2 → very high probability
    p = _prob_above(76.0, 65.99)
    assert p > 0.99


def test_prob_above_when_forecast_well_below_threshold():
    # Forecast 10°F below threshold with σ=2 → very low probability
    p = _prob_above(55.0, 65.99)
    assert p < 0.01


def test_prob_above_symmetry():
    # P(above by X) + P(above by -X) should equal ~1
    p_high = _prob_above(70.0, 65.99)
    p_low  = _prob_above(61.98, 65.99)  # symmetric around 65.99
    assert abs(p_high + p_low - 1.0) < 1e-6


def test_prob_above_one_sigma_above():
    # Forecast 1σ above threshold → ~84%
    p = _prob_above(65.99 + FORECAST_STD_F, 65.99)
    assert abs(p - 0.8413) < 0.001


def test_prob_above_one_sigma_below():
    # Forecast 1σ below threshold → ~16%
    p = _prob_above(65.99 - FORECAST_STD_F, 65.99)
    assert abs(p - 0.1587) < 0.001


# ----------------------------------------------------------------- strategy

def test_returns_estimate_for_temp_ticker():
    strategy = WeatherStrategy()
    with patch.object(strategy._feed, "fetch_nyc_temperature_f", return_value=70.0):
        estimates = strategy.estimate_edge({"KXTEMPNYCH-26APR0319-T65.99": make_market()})
    assert len(estimates) == 1
    assert estimates[0].ticker == "KXTEMPNYCH-26APR0319-T65.99"
    assert estimates[0].yes_probability > 0.9  # 70°F >> 65.99°F threshold


def test_skips_non_temp_tickers():
    strategy = WeatherStrategy()
    market = make_market(ticker="KXMLBHIT-26APR031610CHCCLE-CLEGARIAS13-1")
    estimates = strategy.estimate_edge({market.ticker: market})
    assert estimates == []


def test_skips_market_when_forecast_unavailable():
    strategy = WeatherStrategy()
    with patch.object(strategy._feed, "fetch_nyc_temperature_f", return_value=None):
        estimates = strategy.estimate_edge({"KXTEMPNYCH-26APR0319-T65.99": make_market()})
    assert estimates == []


def test_returns_no_estimates_for_empty_markets():
    strategy = WeatherStrategy()
    assert strategy.estimate_edge({}) == []


def test_confidence_is_set():
    strategy = WeatherStrategy()
    with patch.object(strategy._feed, "fetch_nyc_temperature_f", return_value=70.0):
        estimates = strategy.estimate_edge({"KXTEMPNYCH-26APR0319-T65.99": make_market()})
    assert estimates[0].confidence == 0.5
