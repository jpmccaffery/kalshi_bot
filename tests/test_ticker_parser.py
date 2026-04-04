from datetime import datetime
from src.kalshi.ticker_parser import parse_nyc_temp_ticker


def test_parses_valid_ticker():
    result = parse_nyc_temp_ticker("KXTEMPNYCH-26APR0319-T65.99")
    assert result is not None
    assert result.ticker == "KXTEMPNYCH-26APR0319-T65.99"
    assert result.dt == datetime(2026, 4, 3, 19)
    assert result.threshold_f == 65.99


def test_parses_different_month():
    result = parse_nyc_temp_ticker("KXTEMPNYCH-26JUN1514-T80.00")
    assert result.dt == datetime(2026, 6, 15, 14)
    assert result.threshold_f == 80.00


def test_parses_midnight_hour():
    result = parse_nyc_temp_ticker("KXTEMPNYCH-26JAN0100-T32.00")
    assert result.dt == datetime(2026, 1, 1, 0)


def test_returns_none_for_wrong_series():
    assert parse_nyc_temp_ticker("KXMLBHIT-26APR031610CHCCLE-CLEGARIAS13-1") is None


def test_returns_none_for_garbage():
    assert parse_nyc_temp_ticker("not-a-ticker") is None


def test_returns_none_for_empty_string():
    assert parse_nyc_temp_ticker("") is None


def test_returns_none_for_invalid_month():
    assert parse_nyc_temp_ticker("KXTEMPNYCH-26XYZ0319-T65.99") is None


def test_threshold_precision():
    result = parse_nyc_temp_ticker("KXTEMPNYCH-26APR0319-T72.99")
    assert result.threshold_f == 72.99
