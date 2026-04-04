"""
Weather data feed using Open-Meteo (https://open-meteo.com).
Free, no API key required. Returns hourly temperature forecasts.
"""

import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_NYC_LAT = 40.7128
_NYC_LON = -74.0060


class WeatherFeed:
    def fetch_nyc_temperature_f(self, dt: datetime) -> Optional[float]:
        """
        Return the forecasted temperature in °F for NYC at the given hour.
        dt should be naive NYC local time (matching the Kalshi ticker).
        Returns None if the forecast is unavailable for that hour.
        """
        try:
            resp = requests.get(_BASE_URL, params={
                "latitude": _NYC_LAT,
                "longitude": _NYC_LON,
                "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "timezone": "America/New_York",
                "forecast_days": 7,
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Weather fetch failed: %s", e)
            return None

        target = dt.strftime("%Y-%m-%dT%H:00")
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]

        for time_str, temp in zip(times, temps):
            if time_str == target:
                return temp

        logger.warning("No forecast found for %s", target)
        return None
