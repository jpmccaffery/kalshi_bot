"""
Shared data loading for plot_return_by_hour.py and plot_analysis.py.
"""
from __future__ import annotations

import csv
import datetime
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent.parent

UPDATE_HOURS = (1, 7, 13, 19)

_EXPIRY_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_CITY_TZ: dict[str, str] = {
    "NY":    "America/New_York",
    "BOS":   "America/New_York",
    "DC":    "America/New_York",
    "MIA":   "America/New_York",
    "ATL":   "America/New_York",
    "PHIL":  "America/New_York",
    "PHILA": "America/New_York",
    "CHI":   "America/Chicago",
    "DAL":   "America/Chicago",
    "HOU":   "America/Chicago",
    "MIN":   "America/Chicago",
    "OKC":   "America/Chicago",
    "NOLA":  "America/Chicago",
    "SAT":   "America/Chicago",
    "SATX":  "America/Chicago",
    "AUS":   "America/Chicago",
    "DEN":   "America/Denver",
    "PHX":   "America/Phoenix",
    "LAX":   "America/Los_Angeles",
    "SFO":   "America/Los_Angeles",
    "SEA":   "America/Los_Angeles",
    "LAS":   "America/Los_Angeles",
    "LV":    "America/Los_Angeles",
}

_TZ_LABEL = {
    "America/New_York":    "Eastern",
    "America/Chicago":     "Central",
    "America/Denver":      "Mountain",
    "America/Phoenix":     "Mountain",
    "America/Los_Angeles": "Pacific",
}

_TICKER_RE = re.compile(r"^KX(?:HIGH|HIGHT|LOW|LOWT)([A-Z]+)-")


def ticker_tz_label(ticker: str) -> str:
    """Return e.g. 'Eastern', 'Central', 'Mountain', 'Pacific', or 'Unknown'."""
    tz = ticker_tz(ticker)
    if tz is None:
        return "Unknown"
    return _TZ_LABEL.get(str(tz), "Unknown")


def ticker_tz(ticker: str) -> ZoneInfo | None:
    m = _TICKER_RE.match(ticker)
    if not m:
        return None
    tz_name = _CITY_TZ.get(m.group(1))
    return ZoneInfo(tz_name) if tz_name else None


def parse_expiry(expiry: str) -> datetime.date | None:
    try:
        return datetime.date(
            2000 + int(expiry[:2]),
            _EXPIRY_MONTHS[expiry[2:5].upper()],
            int(expiry[5:]),
        )
    except (ValueError, KeyError):
        return None


def local_hour_and_days_out(buy_date_str: str, expiry_str: str,
                             tz: ZoneInfo) -> tuple[int, float]:
    utc_dt = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d %H:%M").replace(
        tzinfo=datetime.timezone.utc
    )
    local_dt = utc_dt.astimezone(tz)
    exp_date = parse_expiry(expiry_str)
    days_out = (exp_date - local_dt.date()).days if exp_date else 0
    return local_dt.hour, float(max(days_out, 0))


def staleness(buy_date_str: str) -> float:
    h = int(buy_date_str[11:13])
    m = int(buy_date_str[14:16])
    buy_h = h + m / 60.0
    candidates = [u for u in UPDATE_HOURS if u <= buy_h]
    last = max(candidates) if candidates else max(UPDATE_HOURS) - 24
    return buy_h - last


def find_csv(base: Path) -> Path:
    direct = base / "transaction_summary.csv"
    if direct.exists():
        return direct
    global_csv = REPO_ROOT / "output" / "transaction_summary.csv"
    if global_csv.exists():
        return global_csv
    raise FileNotFoundError(f"No transaction_summary.csv found under {base}")


def load(path: Path) -> list[dict]:
    """Load closed positions from a transaction_summary.csv.

    Exits with an error if any expired position is missing settlement data.
    """
    today = datetime.date.today()
    rows: list[dict] = []
    missing: list[str] = []

    with path.open() as f:
        for r in csv.DictReader(f):
            exit_type = r.get("exit_type", "")
            expiry    = r.get("expiry", "")
            ticker    = r.get("ticker", "")

            if not exit_type:
                exp_date = parse_expiry(expiry)
                if exp_date and exp_date < today:
                    missing.append(f"  {ticker} (expired {exp_date})")
                continue

            try:
                ret      = float(r["return_pct"])
                buy_date = r["buy_date"]
                days_out = float(r.get("days_out", 0))
            except (ValueError, KeyError):
                continue

            tz = ticker_tz(ticker)
            if tz is not None:
                loc_hour, loc_days_out = local_hour_and_days_out(buy_date, expiry, tz)
            else:
                loc_hour, loc_days_out = int(buy_date[11:13]), days_out

            rows.append({
                "symbol":         ticker,
                "buy_date":       buy_date,
                "expiry":         expiry,
                "hour":           int(buy_date[11:13]),
                "local_hour":     loc_hour,
                "staleness":      staleness(buy_date),
                "days_out":       days_out,
                "local_days_out": loc_days_out,
                "tz_label":       ticker_tz_label(ticker),
                "return_pct":     ret,
                "outcome":        exit_type,
                "exit_type":      exit_type,
                "pnl":            float(r.get("pnl", 0)),
                "edge":           float(r.get("edge", 0)),
                "cost":           float(r.get("cost", 0)),
                "quantity":       float(r.get("quantity", 0)),
            })

    if missing:
        print(f"ERROR: {len(missing)} expired position(s) missing settlement data:")
        for m in missing:
            print(m)
        print("\nRe-run transaction_summary.py to pick up settlements.")
        sys.exit(1)

    return rows
