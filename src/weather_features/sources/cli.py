"""
CLI (Daily Climate Report) source client for the weather features pipeline.

Fetches NWS CLI text products via IEM for each station, parsing high, low,
average temperature and precipitation for each observation date.

API: https://mesonet.agron.iastate.edu/api/1/nwstext_search.json
     ?sts=<start>&ets=<end>&awips=CLI<3char>

Polls once per hour (min_poll_interval_sec=3600) — CLI reports are daily.

Produces observations rows with CLI-specific fields populated.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
from typing import Optional

import aiohttp

from .base import SourceClient

log = logging.getLogger(__name__)

IEM_AFOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
USER_AGENT = "kalshi-weather-pipeline/1.0 jpmccaffery@gmail.com"

# Map ICAO → 3-character AWIPS suffix for CLI products
CLI_CODES = {
    "KNYC": "NYC",
    "KMIA": "MIA",
    "KPHL": "PHL",
    "KBOS": "BOS",
    "KATL": "ATL",
    "KDCA": "DCA",
    "KORD": "ORD",
    "KHOU": "HOU",
    "KDFW": "DFW",
    "KAUS": "AUS",
    "KSAT": "SAT",
    "KMSY": "MSY",
    "KMSP": "MSP",
    "KOKC": "OKC",
    "KDEN": "DEN",
    "KPHX": "PHX",
    "KLAX": "LAX",
    "KSFO": "SFO",
    "KSEA": "SEA",
    "KLAS": "LAS",
}

# CLI text format: NWS narrative climate summary
# Example date line: "...THE ATLANTA CLIMATE SUMMARY FOR MAY 18 2026..."
_DATE_RE = re.compile(
    r"CLIMATE\s+(?:SUMMARY|REPORT|STATEMENT)\s+FOR\s+(\w+)\s+(\d{1,2})[,\s]+(\d{4})",
    re.IGNORECASE,
)
# WMO header: "CDUS42 KFFC 190031" → day=19, hour=00, minute=31
_WMO_RE = re.compile(r'\w{6}\s+\w{4}\s+(\d{2})(\d{2})(\d{2})')

# Temperature: "MAXIMUM         87" or "MAXIMUM          87   5:34 PM"
_MAX_RE = re.compile(r'MAXIMUM\s+([-\d]+)', re.IGNORECASE)
_MIN_RE = re.compile(r'MINIMUM\s+([-\d]+)', re.IGNORECASE)
_AVG_RE = re.compile(r'AVERAGE\s+([-\d]+)', re.IGNORECASE)
# Precipitation: header is "PRECIPITATION (IN)" then value on "  TODAY   0.00" line
# Also handles inline formats like "PRECIPITATION   0.00" or "TOTAL PRECIP   T"
_PRECIP_RE = re.compile(
    r'(?:PRECIPITATION|RAINFALL|TOTAL PRECIP).*?\n\s+TODAY\s+(T|[\d.]+)'
    r'|(?:PRECIPITATION|RAINFALL|TOTAL PRECIP)\s+[:\s]*(T|[\d.]+)',
    re.IGNORECASE | re.DOTALL,
)
_SNOW_RE = re.compile(
    r'SNOWFALL.*?\n\s+TODAY\s+(T|[\d.]+)'
    r'|SNOWFALL\s+[:\s]*(T|[\d.]+)',
    re.IGNORECASE | re.DOTALL,
)

_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


def _compute_hash(data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def _parse_precip(val_str: str) -> Optional[float]:
    """Parse precipitation value: 'T' = trace = 0.001, else float inches."""
    val_str = val_str.strip()
    if val_str.upper() == "T":
        return 0.001
    try:
        return float(val_str)
    except ValueError:
        return None


def _parse_cli_report(
    cli_text: str,
    station_icao: str,
    city: str,
    issuance_time: dt.datetime,
    raw_hash: str,
    now: dt.datetime,
) -> Optional[dict]:
    """Parse a single CLI text product into an observations row."""
    # Extract observation date from body text: "CLIMATE SUMMARY FOR MAY 18 2026"
    obs_date: Optional[dt.date] = None
    date_m = _DATE_RE.search(cli_text)
    if date_m:
        month_str = date_m.group(1).upper()[:3]
        month = _MONTH_ABBR.get(month_str) or _MONTH_ABBR.get(date_m.group(1).upper())
        if month:
            try:
                obs_date = dt.date(int(date_m.group(3)), month, int(date_m.group(2)))
            except ValueError:
                pass

    if obs_date is None:
        # Fall back: date from WMO header day, assume previous day if issued near midnight
        wmo_m = _WMO_RE.search(cli_text)
        if wmo_m:
            try:
                day = int(wmo_m.group(1))
                obs_date = issuance_time.date().replace(day=day)
            except ValueError:
                pass

    if obs_date is None:
        log.debug("CLI: could not parse date from %s text", station_icao)
        return None

    # Extract temperature values
    high_m = _MAX_RE.search(cli_text)
    low_m  = _MIN_RE.search(cli_text)
    avg_m  = _AVG_RE.search(cli_text)
    precip_m = _PRECIP_RE.search(cli_text)
    snow_m   = _SNOW_RE.search(cli_text)

    high_f:    Optional[float] = float(high_m.group(1)) if high_m else None
    low_f:     Optional[float] = float(low_m.group(1))  if low_m  else None
    avg_f:     Optional[float] = float(avg_m.group(1))  if avg_m  else None
    precip_in: Optional[float] = _parse_precip(next(g for g in precip_m.groups() if g is not None)) if precip_m else None
    snow_in:   Optional[float] = _parse_precip(next(g for g in snow_m.groups()   if g is not None)) if snow_m   else None

    text_upper = cli_text.upper()
    is_preliminary = "PRELIMINARY" in text_upper or obs_date == now.date()

    return {
        "poll_time": now,
        "source": "CLI",
        "station_icao": station_icao,
        "city": city,
        # METAR fields — null for CLI rows
        "observation_time": None,
        "temp_f": None,
        "dewpoint_f": None,
        "wind_mph": None,
        "wind_dir_deg": None,
        "pressure_mb": None,
        "sky_cover_pct": None,
        "precip_in_1h": None,
        "raw_metar": None,
        "is_speci": None,
        # CLI fields
        "observation_date": obs_date,
        "high_f": float(high_f) if high_f is not None else None,
        "low_f": float(low_f) if low_f is not None else None,
        "avg_f": float(avg_f) if avg_f is not None else None,
        "precip_in": float(precip_in) if precip_in is not None else None,
        "snow_in": float(snow_in) if snow_in is not None else None,
        "issuance_time": issuance_time,
        "is_preliminary": is_preliminary,
        "raw_text": cli_text,
        "raw_payload_hash": raw_hash,
        "schema_version": 1,
    }


def _parse_iso(s: str) -> Optional[dt.datetime]:
    """Parse ISO datetime, ensuring UTC."""
    if not s:
        return None
    try:
        ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts.astimezone(dt.timezone.utc)
    except ValueError:
        return None


class CLISource(SourceClient):
    name = "CLI"
    table = "observations"
    min_poll_interval_sec = 3600  # Once per hour — CLI reports are daily

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch CLI daily climate reports for all stations.

        Queries the last 48 hours for each station's CLI AWIPS product.
        Parses CLI text and returns one observations row per report found.
        """
        self._update_last_poll(now)
        rows: list[dict] = []

        # 48-hour window
        window_start = now - dt.timedelta(hours=48)
        sts = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        ets = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        headers = {"User-Agent": USER_AGENT}

        async def fetch_station(station, session: aiohttp.ClientSession):
            cli_suffix = CLI_CODES.get(station.icao)
            if not cli_suffix:
                return []

            pil = f"CLI{cli_suffix}"
            # Fetch last 3 reports (~3 days) — CLI is daily so this covers the window
            params = {"pil": pil, "limit": "3", "fmt": "text"}

            try:
                async with self._semaphore if self._semaphore else asyncio.nullcontext():
                    async with session.get(
                        IEM_AFOS_URL, params=params, headers=headers
                    ) as resp:
                        resp.raise_for_status()
                        raw_text = await resp.text()
            except Exception as exc:
                log.warning("CLI fetch failed for %s: %s", station.icao, exc)
                return []

            if not raw_text.strip():
                return []

            raw_hash = _compute_hash({"pil": pil, "text": raw_text})

            # Multiple products are concatenated in the response.
            # Split on WMO header lines (start with digit, e.g. "410 ") or "$$"
            # Each product block starts with a WMO bulletin header.
            import re as _re
            # Split on lines that look like WMO headers (3-digit number + spaces)
            product_blocks = _re.split(r'\n(?=\d{3}\s*\n)', raw_text.strip())

            station_rows = []
            for block in product_blocks:
                block = block.strip()
                if not block:
                    continue
                # Issuance time: parse from the WMO header line "CDUS42 KFFC 190031"
                # Format: TTAAII CCCC DDHHMM
                wmo_m = _re.search(r'\w{6}\s+\w{4}\s+(\d{2})(\d{2})(\d{2})', block)
                if wmo_m:
                    day = int(wmo_m.group(1))
                    hour = int(wmo_m.group(2))
                    minute = int(wmo_m.group(3))
                    # Assume current month/year; handle month boundary
                    try:
                        issuance_time = now.replace(
                            day=day, hour=hour, minute=minute, second=0, microsecond=0
                        )
                        if issuance_time > now:
                            # Must be previous month
                            import calendar
                            prev_month = now.month - 1 or 12
                            prev_year = now.year if now.month > 1 else now.year - 1
                            last_day = calendar.monthrange(prev_year, prev_month)[1]
                            issuance_time = issuance_time.replace(
                                year=prev_year, month=prev_month,
                                day=min(day, last_day)
                            )
                    except ValueError:
                        issuance_time = now
                else:
                    issuance_time = now

                row = _parse_cli_report(
                    block, station.icao, station.market_city,
                    issuance_time, raw_hash, now,
                )
                if row is not None:
                    station_rows.append(row)

            return station_rows

        async with aiohttp.ClientSession() as session:
            # Serialize to avoid rate-limiting IEM
            results = []
            for s in stations:
                result = await fetch_station(s, session)
                results.append(result)
                await asyncio.sleep(0.15)

        for station_rows in results:
            rows.extend(station_rows)

        log.info("CLI: %d rows from %d stations", len(rows), len(stations))
        return rows
