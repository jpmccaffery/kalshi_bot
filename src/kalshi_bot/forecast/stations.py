"""
City -> NWS settlement station mapping for Kalshi temperature markets.

Kalshi settles against the NWS Daily Climate Report (CLI) for a specific
station. The key non-obvious one: NYC settles on Central Park (KNYC), not
LGA or JFK.

Verified against Kalshi contract rules visible in list_markets output.
tz_standard_offset is always LST (non-DST) since NWS CLI uses LST year-round.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Station:
    market_city: str        # Key used throughout the bot
    icao: str               # 4-letter ICAO for NBM station lookup
    name: str               # Human-readable station name
    lat: float              # Sensor lat, for NWS /points lookup
    lon: float              # Sensor lon
    tz_standard_offset: int # Hours behind UTC in standard (non-DST) time


STATIONS = {
    # Eastern (LST = UTC-5)
    "new_york":      Station("new_york",      "KNYC", "Central Park, NY",         40.7794,  -73.9692,  -5),
    "miami":         Station("miami",         "KMIA", "Miami Intl, FL",            25.7906,  -80.3164,  -5),
    "philadelphia":  Station("philadelphia",  "KPHL", "Philadelphia Intl, PA",     39.8744,  -75.2424,  -5),
    "boston":        Station("boston",        "KBOS", "Boston Logan, MA",          42.3606,  -71.0097,  -5),
    "atlanta":       Station("atlanta",       "KATL", "Hartsfield-Jackson, GA",    33.6407,  -84.4277,  -5),
    "washington_dc": Station("washington_dc", "KDCA", "Reagan National, DC",       38.8521,  -77.0377,  -5),

    # Central (LST = UTC-6)
    "chicago":       Station("chicago",       "KORD", "Chicago O'Hare, IL",        41.9603,  -87.9314,  -6),
    "houston":       Station("houston",       "KHOU", "Houston Hobby, TX",         29.6454,  -95.2789,  -6),
    "dallas":        Station("dallas",        "KDFW", "Dallas-Fort Worth, TX",     32.8998, -97.0403,   -6),
    "austin":        Station("austin",        "KAUS", "Austin-Bergstrom, TX",      30.1830,  -97.6799,  -6),
    "san_antonio":   Station("san_antonio",   "KSAT", "San Antonio Intl, TX",      29.5337,  -98.4698,  -6),
    "new_orleans":   Station("new_orleans",   "KMSY", "Louis Armstrong, LA",       29.9934,  -90.2580,  -6),
    "minneapolis":   Station("minneapolis",   "KMSP", "Minneapolis-St Paul, MN",   44.8848,  -93.2223,  -6),
    "oklahoma_city": Station("oklahoma_city", "KOKC", "Will Rogers World, OK",     35.3931,  -97.6007,  -6),

    # Mountain (LST = UTC-7)
    "denver":        Station("denver",        "KDEN", "Denver Intl, CO",           39.8467, -104.6562,  -7),
    "phoenix":       Station("phoenix",       "KPHX", "Phoenix Sky Harbor, AZ",    33.4373, -112.0078,  -7),

    # Pacific (LST = UTC-8)
    "los_angeles":   Station("los_angeles",   "KLAX", "Los Angeles Intl, CA",      33.9381, -118.3889,  -8),
    "san_francisco": Station("san_francisco", "KSFO", "San Francisco Intl, CA",    37.6189, -122.3750,  -8),
    "seattle":       Station("seattle",       "KSEA", "Seattle-Tacoma, WA",        47.4502, -122.3088,  -8),
    "las_vegas":     Station("las_vegas",     "KLAS", "Harry Reid Intl, NV",       36.0840, -115.1537,  -8),
}


def get_station(city: str) -> Optional[Station]:
    return STATIONS.get(city)
