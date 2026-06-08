"""
METAR observation source client for the weather features pipeline.

Fetches recent METAR observations for each station using two APIs:
1. IEM ASOS history API (last 2 hours) — catches SPECI updates and corrections.
2. NWS api.weather.gov latest observation — backup / cross-check.

Strategy: every poll, pull the last 2 hours via IEM ASOS for each station.
Produce one row per (station, observation_time) per poll.

Produces observations rows with METAR-specific fields populated.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import logging
from typing import Optional

import aiohttp

from .base import SourceClient

log = logging.getLogger(__name__)

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
NWS_OBS_URL = "https://api.weather.gov/stations/{icao}/observations/latest"
USER_AGENT = "kalshi-weather-pipeline/1.0 jpmccaffery@gmail.com"

KNOTS_TO_MPH = 1.15078

# Sky cover code → percent
SKY_CODE_PCT = {
    "CLR": 0.0,
    "SKC": 0.0,
    "CAVOK": 0.0,
    "FEW": 12.5,
    "SCT": 37.5,
    "BKN": 62.5,
    "OVC": 100.0,
    "VV": 100.0,
    "OVX": 100.0,
}


def _sky_code_to_pct(code: Optional[str]) -> Optional[float]:
    if not code:
        return None
    return SKY_CODE_PCT.get(code.strip().upper())


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return None if (f != f) else f  # NaN check
    except (ValueError, TypeError):
        return None


def _compute_hash(data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def _parse_iso(s: str) -> Optional[dt.datetime]:
    """Parse ISO datetime string, ensuring UTC."""
    if not s:
        return None
    try:
        ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _parse_iem_asos_csv(csv_text: str, station_icao: str,
                        raw_hash: str, now: dt.datetime,
                        city: str) -> list[dict]:
    """
    Parse IEM ASOS CSV response into observation rows.

    Expected columns (selected via data=all):
    station,valid,lon,lat,tmpf,dwpf,relh,drct,sknt,p01i,alti,mslp,
    vsby,gust,skyc1,skyc2,skyc3,skyc4,skyl1,skyl2,skyl3,skyl4,
    wxcodes,ice_accretion_1hr,ice_accretion_3hr,ice_accretion_6hr,
    peak_wind_gust,peak_wind_drct,peak_wind_time,feel,metar,snowdepth
    """
    rows = []
    lines = csv_text.strip().splitlines()
    if not lines:
        return rows

    # Skip comment lines (start with #)
    lines = [ln for ln in lines if not ln.startswith("#")]
    if not lines:
        return rows

    header_line = lines[0]
    headers = [h.strip() for h in header_line.split(",")]

    for line in lines[1:]:
        if not line.strip():
            continue
        vals = line.split(",")
        if len(vals) < len(headers):
            # Pad with empty strings
            vals.extend([""] * (len(headers) - len(vals)))
        row_dict = {headers[i]: vals[i].strip() for i in range(len(headers))}

        # Parse valid time
        valid_str = row_dict.get("valid", "")
        if not valid_str:
            continue
        try:
            # IEM ASOS valid is UTC: "2026-05-18 12:00"
            obs_time = dt.datetime.strptime(valid_str, "%Y-%m-%d %H:%M")
            obs_time = obs_time.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            obs_time = _parse_iso(valid_str)
            if obs_time is None:
                continue

        # Temperature (°F)
        temp_f = _safe_float(row_dict.get("tmpf"))

        # Dewpoint (°F)
        dewpoint_f = _safe_float(row_dict.get("dwpf"))

        # Wind speed: knots → mph
        sknt = _safe_float(row_dict.get("sknt"))
        wind_mph = (sknt * KNOTS_TO_MPH) if sknt is not None else None

        # Wind direction
        wind_dir_deg = _safe_float(row_dict.get("drct"))

        # Pressure: altimeter in inHg → mb (hPa)
        alti = _safe_float(row_dict.get("alti"))
        pressure_mb = (alti * 33.8639) if alti is not None else None
        # mslp is already in mb
        if pressure_mb is None:
            pressure_mb = _safe_float(row_dict.get("mslp"))

        # Sky cover: use first non-empty skyc field
        sky_cover_pct: Optional[float] = None
        for sky_field in ("skyc1", "skyc2", "skyc3", "skyc4"):
            code = row_dict.get(sky_field, "").strip()
            if code and code not in ("M", ""):
                pct = _sky_code_to_pct(code)
                if pct is not None:
                    sky_cover_pct = pct
                    break

        # Precip 1-hour (inches)
        precip_in_1h = _safe_float(row_dict.get("p01i"))

        # Raw METAR string
        raw_metar = row_dict.get("metar", "").strip() or None

        # SPECI check
        is_speci = bool(raw_metar and "SPECI" in raw_metar.upper())

        rows.append({
            "poll_time": now,
            "source": "METAR",
            "station_icao": station_icao,
            "city": city,
            "observation_time": obs_time,
            "temp_f": float(temp_f) if temp_f is not None else None,
            "dewpoint_f": float(dewpoint_f) if dewpoint_f is not None else None,
            "wind_mph": float(wind_mph) if wind_mph is not None else None,
            "wind_dir_deg": float(wind_dir_deg) if wind_dir_deg is not None else None,
            "pressure_mb": float(pressure_mb) if pressure_mb is not None else None,
            "sky_cover_pct": float(sky_cover_pct) if sky_cover_pct is not None else None,
            "precip_in_1h": float(precip_in_1h) if precip_in_1h is not None else None,
            "raw_metar": raw_metar,
            "is_speci": is_speci,
            "raw_payload_hash": raw_hash,
            "schema_version": 1,
        })

    return rows


def _parse_nws_latest(data: dict, station_icao: str, city: str,
                      raw_hash: str, now: dt.datetime) -> Optional[dict]:
    """Parse NWS GeoJSON latest observation into an observations row."""
    props = data.get("properties", {})

    obs_time = _parse_iso(props.get("timestamp", ""))
    if obs_time is None:
        return None

    # Temperature: °C → °F
    temp_c = _safe_float(
        props.get("temperature", {}).get("value") if isinstance(props.get("temperature"), dict) else None
    )
    temp_f = ((temp_c * 9 / 5) + 32) if temp_c is not None else None

    # Dewpoint: °C → °F
    dew_c = _safe_float(
        props.get("dewpoint", {}).get("value") if isinstance(props.get("dewpoint"), dict) else None
    )
    dewpoint_f = ((dew_c * 9 / 5) + 32) if dew_c is not None else None

    # Wind speed: km/h → mph
    wind_kmh = _safe_float(
        props.get("windSpeed", {}).get("value") if isinstance(props.get("windSpeed"), dict) else None
    )
    wind_mph = (wind_kmh * 0.621371) if wind_kmh is not None else None

    # Wind direction
    wind_dir_deg = _safe_float(
        props.get("windDirection", {}).get("value") if isinstance(props.get("windDirection"), dict) else None
    )

    # Pressure: Pa → mb
    pressure_pa = _safe_float(
        props.get("barometricPressure", {}).get("value") if isinstance(props.get("barometricPressure"), dict) else None
    )
    pressure_mb = (pressure_pa / 100.0) if pressure_pa is not None else None

    # Sky cover from cloudLayers
    cloud_layers = props.get("cloudLayers", [])
    sky_cover_pct: Optional[float] = None
    if cloud_layers and isinstance(cloud_layers, list):
        # Use lowest layer
        layer = cloud_layers[0] if cloud_layers else {}
        amount = layer.get("amount", "") if isinstance(layer, dict) else ""
        sky_cover_pct = _sky_code_to_pct(amount)

    raw_metar = props.get("rawMessage") or None
    is_speci = bool(raw_metar and "SPECI" in str(raw_metar).upper())

    return {
        "poll_time": now,
        "source": "METAR",
        "station_icao": station_icao,
        "city": city,
        "observation_time": obs_time,
        "temp_f": float(temp_f) if temp_f is not None else None,
        "dewpoint_f": float(dewpoint_f) if dewpoint_f is not None else None,
        "wind_mph": float(wind_mph) if wind_mph is not None else None,
        "wind_dir_deg": float(wind_dir_deg) if wind_dir_deg is not None else None,
        "pressure_mb": float(pressure_mb) if pressure_mb is not None else None,
        "sky_cover_pct": float(sky_cover_pct) if sky_cover_pct is not None else None,
        "precip_in_1h": None,
        "raw_metar": str(raw_metar) if raw_metar else None,
        "is_speci": is_speci,
        "raw_payload_hash": raw_hash,
        "schema_version": 1,
    }


class METARSource(SourceClient):
    name = "METAR"
    table = "observations"
    min_poll_interval_sec = 600

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        super().__init__(semaphore)

    async def poll(self, now: dt.datetime, stations: list) -> list[dict]:
        """
        Fetch recent METAR observations for all stations.

        Uses IEM ASOS batch request for all stations at once (avoids rate limiting).
        Also fetches NWS latest for each station concurrently as a fallback.
        Deduplicates by observation_time within each station — IEM wins over NWS.
        """
        self._update_last_poll(now)
        rows: list[dict] = []

        # Time window: last 2 hours
        window_start = now - dt.timedelta(hours=2)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/geo+json",
        }

        # IEM ASOS batch: all stations in one request
        iem_rows_by_station: dict[str, dict[dt.datetime, dict]] = {
            s.icao: {} for s in stations
        }
        iem_params = [
            ("data", "all"), ("tz", "UTC"), ("format", "onlycomma"), ("latlon", "no"),
            ("year1", str(window_start.year)), ("month1", str(window_start.month)),
            ("day1", str(window_start.day)), ("hour1", str(window_start.hour)),
            ("minute1", str(window_start.minute)),
            ("year2", str(now.year)), ("month2", str(now.month)),
            ("day2", str(now.day)), ("hour2", str(now.hour)),
            ("minute2", str(now.minute)),
        ] + [("station", s.icao) for s in stations]

        station_map = {s.icao: s for s in stations}

        async with aiohttp.ClientSession() as session:
            # 1. Single batched IEM request for all stations
            try:
                async with session.get(
                    IEM_ASOS_URL, params=iem_params,
                    headers={"User-Agent": USER_AGENT}
                ) as resp:
                    resp.raise_for_status()
                    csv_text = await resp.text()

                iem_hash = hashlib.sha256(csv_text.encode()).hexdigest()
                lines = [ln for ln in csv_text.strip().splitlines() if not ln.startswith("#")]
                data_lines = len(lines) - 1 if lines else 0
                if data_lines == 0:
                    log.warning("METAR IEM batch returned 0 data rows (empty response)")
                else:
                    log.debug("METAR IEM batch: %d data rows", data_lines)
                if lines:
                    hdr = [h.strip() for h in lines[0].split(",")]
                    for line in lines[1:]:
                        if not line.strip():
                            continue
                        vals = line.split(",")
                        if len(vals) < len(hdr):
                            vals.extend([""] * (len(hdr) - len(vals)))
                        row_dict = {hdr[i]: vals[i].strip() for i in range(len(hdr))}
                        icao = row_dict.get("station", "").strip()
                        st = station_map.get(icao)
                        if st is None:
                            continue
                        # Parse into rows using existing helper
                        parsed = _parse_iem_asos_csv(
                            hdr[0] + "," + ",".join(hdr[1:]) + "\n" + line,
                            icao, iem_hash, now, st.market_city
                        )
                        for r in parsed:
                            obs_t = r["observation_time"]
                            if obs_t:
                                iem_rows_by_station[icao][obs_t] = r
            except Exception as exc:
                log.warning("METAR IEM batch fetch failed: %s", exc)

        async def fetch_station(station, session: aiohttp.ClientSession):
            station_rows_by_time: dict[dt.datetime, dict] = dict(
                iem_rows_by_station.get(station.icao, {})
            )
            # placeholder so the NWS block below compiles
            _ = None

            # 2. NWS latest observation
            nws_url = NWS_OBS_URL.format(icao=station.icao)
            try:
                if self._semaphore:
                    async with self._semaphore:
                        async with session.get(nws_url, headers=headers) as resp:
                            resp.raise_for_status()
                            nws_data = await resp.json(content_type=None)
                else:
                    async with session.get(nws_url, headers=headers) as resp:
                        resp.raise_for_status()
                        nws_data = await resp.json(content_type=None)

                nws_hash = _compute_hash(nws_data)
                nws_row = _parse_nws_latest(
                    nws_data, station.icao, station.market_city, nws_hash, now
                )
                if nws_row:
                    obs_time = nws_row["observation_time"]
                    # Only add if not already covered by IEM (IEM has more fields)
                    if obs_time and obs_time not in station_rows_by_time:
                        station_rows_by_time[obs_time] = nws_row
            except Exception as exc:
                log.warning("METAR NWS fetch failed for %s: %s", station.icao, exc)

            return list(station_rows_by_time.values())

        # 2. NWS concurrent — one per station, no rate limit issues
        async with aiohttp.ClientSession() as session:
            tasks = [fetch_station(s, session) for s in stations]
            results = await asyncio.gather(*tasks)

        for station_rows in results:
            rows.extend(station_rows)

        if rows:
            log.info("METAR: %d rows from %d stations", len(rows), len(stations))
        else:
            log.warning("METAR: 0 rows returned — IEM and NWS both produced nothing")
        return rows
