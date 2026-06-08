"""
Derived views over the raw data. NOT IMPLEMENTED in this PR — these
are stubs documenting the intended shape of future feature engineering.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional


def daily_summary_from_hourly(
    poll_time: dt.datetime,
    source: str,
    station_icao: str,
    target_date: dt.date,
    kind: str,
    station_tz_offset: int,
) -> Optional[dict]:
    """
    Aggregate one source's hourly forecast trajectory into a daily
    high/low for the given LST calendar date.

    For deterministic sources: returns {"value_f": ...}.
    For ensemble sources: returns {"value_f": mean, "members": [...]}.
    Returns None if the hourly data doesn't cover the target window.

    The LST window for kind="high" on target_date=D is approximately
    [D 00:00 LST, D 24:00 LST]. For kind="low" it is
    [D-1 18:00 LST, D 12:00 LST] (the overnight period ending that
    morning), matching the NWS CLI reporting convention.

    Be careful: this function defines what "daily high/low" means
    everywhere downstream. Don't reimplement it in notebooks.
    """
    raise NotImplementedError


def unified_daily_view(
    poll_time: dt.datetime,
    station_icao: str,
    target_date: dt.date,
    kind: str,
) -> dict[str, dict]:
    """
    Return a dict keyed by source name, where each value is the daily
    summary for that source at poll_time. For native-daily sources,
    reads from daily_forecasts. For hourly sources, calls
    daily_summary_from_hourly.

    This is the table the feature pipeline consumes.
    """
    raise NotImplementedError


def latest_payload(
    poll_time: dt.datetime,
    source: str,
    station_icao: str,
    table: str,
) -> Optional[dict]:
    """
    Find the most recent row written for this source/station as of
    poll_time. Used when we want to ask 'what did source S know at
    time T?' rather than 'what did source S say at exactly poll T?'.
    """
    raise NotImplementedError


def nowcast_high(
    poll_time: dt.datetime,
    station_icao: str,
    target_date: dt.date,
    station_tz_offset: int,
) -> Optional[float]:
    """
    Combine METAR observations (already-realized temps for today) with
    HRRR's remaining hourly forecast to produce the best estimate of
    today's high given everything known at poll_time. Only meaningful
    when poll_time falls within target_date in LST.
    """
    raise NotImplementedError
