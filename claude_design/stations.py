"""
City -> settlement station mapping.

Kalshi temperature markets settle against the NWS Daily Climate Report (CLI)
for a SPECIFIC station. NYC is Central Park (KNYC), not LGA or JFK. We need
the right ICAO so that:

  - The NWS forecast we pull (api.weather.gov /points -> /gridpoints/.../forecast)
    is for the right grid cell (we use the station's lat/lon).
  - The NBM text bulletin we fetch (NBP/NBE) is keyed on that station's call sign.

Verify each entry against:
  - Kalshi's contract spec PDFs for the city (e.g. NHIGH.pdf, NHIGH_LAX.pdf)
  - https://www.weather.gov/wrh/Climate?wfo=<wfo> for the CLI station
  - https://vlab.noaa.gov/web/mdl/nbm-stations for NBM station availability

Lat/lon is the actual sensor lat/lon (used to query NWS gridpoints), not the
geographic centroid of the city.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Station:
    market_city: str        # The city name as it appears in your Kalshi data
    icao: str               # 4-letter ICAO, used as NBM station id
    name: str               # Human-readable station name
    lat: float              # Sensor lat, for NWS /points lookup
    lon: float              # Sensor lon
    tz_standard_offset: int # Hours behind UTC in standard (non-DST) time.
                            # Used to align NBM 12Z/00Z windows to local
                            # calendar days, since NWS CLI uses LST always.


# Starter set. ADD/VERIFY the rest of your 39 cities before going live.
# I've put the ones I'm most confident about; the others need verification
# against Kalshi's actual contract spec PDFs.
STATIONS = {
    "new_york": Station("new_york", "KNYC", "Central Park, NY",
                        40.7794, -73.9692, -5),
    "chicago":  Station("chicago",  "KORD", "Chicago O'Hare, IL",
                        41.9603, -87.9314, -6),
    "los_angeles": Station("los_angeles", "KLAX", "Los Angeles Intl, CA",
                           33.9381, -118.3889, -8),
    "miami":    Station("miami",    "KMIA", "Miami Intl, FL",
                        25.7906, -80.3164, -5),
    "denver":   Station("denver",   "KDEN", "Denver Intl, CO",
                        39.8467, -104.6562, -7),
    "austin":   Station("austin",   "KAUS", "Austin-Bergstrom, TX",
                        30.1830, -97.6799, -6),
    "philadelphia": Station("philadelphia", "KPHL", "Philadelphia Intl, PA",
                            39.8744, -75.2424, -5),
    "boston":   Station("boston",   "KBOS", "Boston Logan, MA",
                        42.3606, -71.0097, -5),
    # ... add the remaining cities here
}


def get_station(city: str) -> Optional[Station]:
    return STATIONS.get(city)
