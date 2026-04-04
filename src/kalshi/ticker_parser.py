"""
Shared utilities for parsing Kalshi ticker strings into structured data.

Ticker format for NYC temperature markets:
  KXTEMPNYCH-{YYMONDDH}-T{threshold}
  e.g. KXTEMPNYCH-26APR0319-T65.99
         year=2026, month=APR, day=03, hour=19, threshold=65.99°F

YES resolves if the observed temperature at that hour is >= threshold.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_NYC_TEMP_RE = re.compile(
    r'^KXTEMPNYCH-(\d{2})([A-Z]{3})(\d{2})(\d{2})-T(\d+\.\d+)$'
)


@dataclass
class NYCTempTicker:
    ticker: str
    dt: datetime       # NYC local time (naive) of the observation hour
    threshold_f: float # temperature threshold in °F; YES resolves if temp >= threshold


def parse_nyc_temp_ticker(ticker: str) -> Optional[NYCTempTicker]:
    """
    Parse a KXTEMPNYCH ticker string. Returns None if the ticker is not
    a recognised NYC temperature market.
    """
    m = _NYC_TEMP_RE.match(ticker)
    if not m:
        return None
    year_str, month_str, day_str, hour_str, threshold_str = m.groups()
    month = _MONTHS.get(month_str)
    if month is None:
        return None
    dt = datetime(2000 + int(year_str), month, int(day_str), int(hour_str))
    return NYCTempTicker(ticker=ticker, dt=dt, threshold_f=float(threshold_str))
