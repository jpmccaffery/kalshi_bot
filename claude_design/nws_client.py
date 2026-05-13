"""
NWS api.weather.gov client.

What we get from NWS:
  - The OFFICIAL local forecast for any lat/lon. This is what NWS forecasters
    publish, derived from NBM + their own adjustments. It's the same data
    on forecast.weather.gov.
  - Updated several times a day, often hourly during active weather.

What we use it for:
  - A deterministic point estimate for daily max/min temperature
  - Sanity-check the NBM 50th percentile against the official forecast;
    when they disagree by more than ~3F, NWS forecasters have manually
    adjusted from the NBM blend, which is a signal worth respecting.
  - Hourly forecast for late-day Bayesian updates (Phase 4 work, not now)

Two-step flow:
  1. GET /points/{lat},{lon}  -> returns the gridpoint URL
  2. GET /gridpoints/{wfo}/{x},{y}  -> raw gridded data, including
     maxTemperature and minTemperature time series

The /forecast endpoint also exists but loses precision (it bins into
"daytime"/"overnight" periods); /gridpoints gives the underlying
hourly+ time series, which is what we want.

Important: NWS requires a User-Agent identifying your app + contact info.
Without it, requests can be 403'd silently.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"

# Set this to something that identifies your bot + a contact email.
# NWS uses it to reach you if your traffic causes a problem.
USER_AGENT = "kalshi-weather-bot/0.1 (your-email@example.com)"

DEFAULT_TIMEOUT = 15  # seconds


class NWSError(Exception):
    pass


@dataclass
class DailyForecast:
    """Deterministic daily high/low for one calendar day."""
    date: dt.date
    high_f: Optional[float]
    low_f: Optional[float]
    issued_at: dt.datetime  # When NWS published this forecast


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


class NWSClient:
    def __init__(self, user_agent: str = USER_AGENT,
                 timeout: int = DEFAULT_TIMEOUT,
                 session: Optional[requests.Session] = None):
        self._session = session or requests.Session()
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/geo+json",
        }
        self._timeout = timeout
        # Cache the gridpoint lookup per (lat,lon); it never changes for a station.
        self._gridpoint_cache: dict[tuple[float, float], str] = {}

    def _get(self, url: str) -> dict:
        r = self._session.get(url, headers=self._headers, timeout=self._timeout)
        if r.status_code != 200:
            raise NWSError(f"NWS GET {url} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _gridpoint_url(self, lat: float, lon: float) -> str:
        # 4-decimal precision is enough; NWS docs say they don't need more.
        key = (round(lat, 4), round(lon, 4))
        if key in self._gridpoint_cache:
            return self._gridpoint_cache[key]
        meta = self._get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
        url = meta["properties"]["forecastGridData"]
        self._gridpoint_cache[key] = url
        return url

    def get_daily_forecasts(self, lat: float, lon: float,
                            station_tz_offset: int) -> list[DailyForecast]:
        """
        Return a list of (date, high_f, low_f) for every day NWS has data for.

        station_tz_offset is the LOCAL STANDARD TIME offset from UTC
        (e.g. -5 for Eastern, -8 for Pacific). Kalshi CLI reports use LST
        regardless of DST, so we bin temperatures by LST calendar date.
        """
        url = self._gridpoint_url(lat, lon)
        data = self._get(url)
        props = data["properties"]
        issued = dt.datetime.fromisoformat(props["updateTime"].replace("Z", "+00:00"))
        max_series = props.get("maxTemperature", {}).get("values", [])
        min_series = props.get("minTemperature", {}).get("values", [])
        max_unit = props.get("maxTemperature", {}).get("uom", "wmoUnit:degC")
        min_unit = props.get("minTemperature", {}).get("uom", "wmoUnit:degC")

        # NWS publishes max/min already binned by local calendar day, but the
        # validTime stamps are UTC. We reassign each value to a date in LST.
        tz = dt.timezone(dt.timedelta(hours=station_tz_offset))

        def to_lst_date(valid_time_iso: str) -> dt.date:
            # validTime format: "2026-04-28T18:00:00+00:00/PT12H"
            start_iso = valid_time_iso.split("/")[0]
            start = dt.datetime.fromisoformat(start_iso).astimezone(tz)
            # The "max" period typically covers daytime; assigning to the date
            # of the period's midpoint is more robust than its start.
            duration_h = _parse_iso_duration_hours(valid_time_iso.split("/")[1])
            mid = start + dt.timedelta(hours=duration_h / 2)
            return mid.date()

        highs: dict[dt.date, float] = {}
        lows: dict[dt.date, float] = {}
        for entry in max_series:
            v = entry["value"]
            if v is None:
                continue
            f = _c_to_f(v) if max_unit.endswith("degC") else v
            highs[to_lst_date(entry["validTime"])] = f
        for entry in min_series:
            v = entry["value"]
            if v is None:
                continue
            f = _c_to_f(v) if min_unit.endswith("degC") else v
            lows[to_lst_date(entry["validTime"])] = f

        all_dates = sorted(set(highs) | set(lows))
        return [DailyForecast(d, highs.get(d), lows.get(d), issued)
                for d in all_dates]


def _parse_iso_duration_hours(s: str) -> float:
    """Parse a simple ISO-8601 duration like 'PT12H', 'PT1H30M', 'P1DT0H'."""
    # We only care about hours and days, and we want this dependency-free.
    s = s.upper().lstrip("P")
    days = 0.0
    hours = 0.0
    minutes = 0.0
    if "T" in s:
        date_part, time_part = s.split("T", 1)
    else:
        date_part, time_part = s, ""
    if date_part.endswith("D"):
        days = float(date_part[:-1])
    if time_part:
        # Walk through 'H' and 'M' suffixes
        buf = ""
        for ch in time_part:
            if ch.isdigit() or ch == ".":
                buf += ch
            elif ch == "H":
                hours = float(buf); buf = ""
            elif ch == "M":
                minutes = float(buf); buf = ""
    return days * 24 + hours + minutes / 60.0
