"""
TemperatureRecommender — uses NWS + NBM forecast data to compute edge on
Kalshi temperature bracket markets.

Replaces the Open-Meteo + fixed-normal-distribution approach with:
  - NWS api.weather.gov for the official deterministic forecast (sanity check)
  - NOAA NBM NBP bulletin for calibrated percentile distributions
  - Piecewise-linear CDF from percentiles for bracket probability math

See src/kalshi_bot/forecast/ for the underlying package.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date, timedelta
from pathlib import Path

from trading_bot.models import DataSchema, MarketSnapshot, Signal

from kalshi_bot.forecast import Contract, Recommender

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Series → (city_key, "high"|"low") mapping
# city_key must be a key in forecast.stations.STATIONS
# ---------------------------------------------------------------------------

_SERIES_TO_CITY_KIND: dict[str, tuple[str, str]] = {
    "KXHIGHNY":   ("new_york",      "high"),
    "KXLOWTNYC":  ("new_york",      "low"),
    "KXHIGHMIA":  ("miami",         "high"),
    "KXLOWTMIA":  ("miami",         "low"),
    "KXHIGHTDC":  ("washington_dc", "high"),
    "KXLOWTDC":   ("washington_dc", "low"),
    "KXHIGHTLV":  ("las_vegas",     "high"),
    "KXLOWTLV":   ("las_vegas",     "low"),
    "KXHIGHTSEA": ("seattle",       "high"),
    "KXLOWTSEA":  ("seattle",       "low"),
    "KXHIGHTMIN": ("minneapolis",   "high"),
    "KXLOWTMIN":  ("minneapolis",   "low"),
    "KXHIGHTOKC": ("oklahoma_city", "high"),
    "KXLOWTOKC":  ("oklahoma_city", "low"),
    "KXHIGHTDAL": ("dallas",        "high"),
    "KXLOWTDAL":  ("dallas",        "low"),
    "KXHIGHPHIL": ("philadelphia",  "high"),
    "KXLOWTPHIL": ("philadelphia",  "low"),
    "KXHIGHTPHX": ("phoenix",       "high"),
    "KXLOWTPHX":  ("phoenix",       "low"),
    "KXHIGHAUS":  ("austin",        "high"),
    "KXLOWTAUS":  ("austin",        "low"),
    "KXHIGHTATL": ("atlanta",       "high"),
    "KXLOWTATL":  ("atlanta",       "low"),
    "KXLOWTSFO":  ("san_francisco", "low"),
    "KXHIGHTSFO": ("san_francisco", "high"),
    "KXLOWTBOS":  ("boston",        "low"),
    "KXHIGHTBOS": ("boston",        "high"),
    "KXLOWTDEN":  ("denver",        "low"),
    "KXHIGHTHOU": ("houston",       "high"),
    "KXLOWTHOU":  ("houston",       "low"),
    "KXLOWTSATX": ("san_antonio",   "low"),
    "KXHIGHTSATX":("san_antonio",   "high"),
    "KXLOWTCHI":  ("chicago",       "low"),
    "KXHIGHCHI":  ("chicago",       "high"),
    "KXHIGHLAX":  ("los_angeles",   "high"),
    "KXLOWTLAX":  ("los_angeles",   "low"),
    "KXHIGHDEN":  ("denver",        "high"),
    "KXHIGHTNOLA":("new_orleans",   "high"),
}

_TICKER_RE = re.compile(
    r"^(?P<series>[A-Z]+)-(?P<expiry>\d{2}[A-Z]{3}\d{2})-(?P<dir>[BT])(?P<strike>\d+(?:\.\d+)?)$"
)

_MONTH = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_expiry(s: str) -> date:
    """'26APR28' → date(2026, 4, 28)  format: YYMMMDD"""
    return date(2000 + int(s[:2]), _MONTH[s[2:5]], int(s[5:]))


class TemperatureRecommender:
    """
    Signals a buy when the NBM-based model probability exceeds the market
    ask (net of taker fee) by at least min_edge.

    Parameters
    ----------
    min_edge:
        Minimum net edge required to signal (after subtracting taker fee).
    min_yes_ask:
        Skip contracts priced below this (avoids penny markets).
    max_per_expiry:
        Maximum signals per (city, expiry, kind) group per tick.
    horizon:
        Expected holding period passed to Signal.
    """

    def __init__(
        self,
        min_edge:       float        = 0.10,
        min_yes_ask:    float        = 0.10,
        max_per_expiry: int          = 1,
        horizon:        timedelta    = timedelta(hours=24),
        output_dir:     Path | None  = None,
    ) -> None:
        self._min_edge       = min_edge
        self._min_yes_ask    = min_yes_ask
        self._max_per_expiry = max_per_expiry
        self._horizon        = horizon
        self._recommender    = Recommender()
        self._output_dir     = Path(output_dir) if output_dir else None
        # Populated each tick for ALL evaluated contracts (not just buy signals).
        # Keyed by Kalshi ticker; used by ModelBasedSellEngine.
        self._model_probs: dict[str, float] = {}

    def get_model_prob(self, ticker: str) -> float | None:
        """Return the most recently computed model probability for a ticker."""
        return self._model_probs.get(ticker)

    @property
    def required_schema(self) -> DataSchema:
        return DataSchema(columns={"yes_ask": "float64", "yes_bid": "float64"})

    def recommend(self, snapshot: MarketSnapshot) -> list[Signal]:
        today   = snapshot.ts.date()
        signals: list[Signal] = []
        self._model_probs.clear()

        # Group markets by (city, expiry_date, kind) — one group = one event
        groups: dict[tuple[str, date, str], list[dict]] = {}
        for _, row in snapshot.bars.iterrows():
            ticker  = row["symbol"]
            yes_ask = float(row["yes_ask"])
            m = _TICKER_RE.match(ticker)
            if not m:
                continue
            series = m.group("series")
            if series not in _SERIES_TO_CITY_KIND:
                continue
            try:
                expiry_date = _parse_expiry(m.group("expiry"))
            except (ValueError, KeyError):
                continue
            if (expiry_date - today).days < 0:
                continue

            city, kind = _SERIES_TO_CITY_KIND[series]

            yes_bid = float(row.get("yes_bid", float("nan")) or float("nan"))
            key = (city, expiry_date, kind)
            groups.setdefault(key, []).append({
                "ticker":    ticker,
                "direction": m.group("dir"),
                "strike":    float(m.group("strike")),
                "yes_ask":   yes_ask,
                "yes_bid":   yes_bid,
            })

        expiry_counts: dict[tuple, int] = {}

        for (city, expiry_date, kind), markets in groups.items():
            # Build Contract list, distinguishing T-tail direction by position
            b_strikes = sorted(c["strike"] for c in markets if c["direction"] == "B")
            min_b = b_strikes[0]  if b_strikes else float("inf")
            max_b = b_strikes[-1] if b_strikes else float("-inf")

            contracts: list[Contract] = []
            ticker_map: dict[str, str] = {}  # label → original ticker
            # Tracks which labels are eligible for buy signals (ask above minimum).
            buy_eligible: set[str] = set()

            for c in markets:
                ask = c["yes_ask"]
                if ask != ask:  # NaN
                    continue
                strike = c["strike"]
                d      = c["direction"]
                ticker = c["ticker"]

                if d == "B":
                    low   = float(int(strike - 0.5))
                    high  = low + 2.0
                    label = f"B{strike}"
                    contracts.append(Contract(label=label, low=low, high=high,
                                              yes_ask=ask))
                elif strike < min_b:
                    label = f"T<{strike}"
                    contracts.append(Contract(label=label, low=None, high=strike,
                                              yes_ask=ask))
                else:
                    label = f"T>{strike}"
                    contracts.append(Contract(label=label, low=strike, high=None,
                                              yes_ask=ask))
                ticker_map[label] = ticker
                if ask >= self._min_yes_ask:
                    buy_eligible.add(label)

            if not contracts:
                continue

            edges = self._recommender.score_contracts(city, expiry_date, kind, contracts)
            if not edges:
                logger.debug("%s %s %s: no forecast data available", city, expiry_date, kind)
                continue

            # Cache model_prob for every evaluated contract — used by ModelBasedSellEngine.
            bid_map: dict[str, float] = {c["ticker"]: c["yes_bid"] for c in markets}
            for edge_row in edges:
                t = ticker_map.get(edge_row.contract)
                if t:
                    self._model_probs[t] = edge_row.model_prob

            days_out = (expiry_date - today).days
            group_key = (city, expiry_date, kind)

            # --- Per-group evaluation table (log + CSV) ---
            header = (f"{city} {kind} {expiry_date}  (days_out={days_out},"
                      f" {len(edges)} contract(s))")
            rows_log  = [header]
            csv_rows  = []
            for edge_row in sorted(edges, key=lambda e: e.edge, reverse=True):
                t      = ticker_map.get(edge_row.contract, "?")
                bid    = bid_map.get(t, float("nan"))
                bid_s  = f"{bid:.3f}" if bid == bid else "  n/a"
                if edge_row.edge >= self._min_edge and edge_row.contract in buy_eligible:
                    signal_tag = "BUY" if expiry_counts.get(group_key, 0) < self._max_per_expiry else "max_per_expiry"
                else:
                    signal_tag = ""
                def _f(v): return f"{v:.1f}" if v is not None else "n/a"
                rows_log.append(
                    f"  {t:<40}"
                    f"  p10={_f(edge_row.forecast_p10):>5}  p25={_f(edge_row.forecast_p25):>5}"
                    f"  p50={_f(edge_row.forecast_p50):>5}  p75={_f(edge_row.forecast_p75):>5}"
                    f"  p90={_f(edge_row.forecast_p90):>5}  mean={_f(edge_row.forecast_mean):>5}"
                    f"  sd={_f(edge_row.forecast_sd):>4}"
                    f"  ask={edge_row.yes_ask:.3f}  bid={bid_s}"
                    f"  model={edge_row.model_prob:.3f}  fee={edge_row.fee:.4f}"
                    f"  edge={edge_row.edge:+.3f}"
                    + (f"  ★{signal_tag}" if signal_tag else "")
                )
                def _csv(v): return round(v, 2) if v is not None else ""
                csv_rows.append({
                    "ts":            snapshot.ts.isoformat(),
                    "city":          city,
                    "kind":          kind,
                    "expiry_date":   expiry_date,
                    "days_out":      days_out,
                    "ticker":        t,
                    "contract":      edge_row.contract,
                    "forecast_p10":  _csv(edge_row.forecast_p10),
                    "forecast_p25":  _csv(edge_row.forecast_p25),
                    "forecast_p50":  _csv(edge_row.forecast_p50),
                    "forecast_p75":  _csv(edge_row.forecast_p75),
                    "forecast_p90":  _csv(edge_row.forecast_p90),
                    "forecast_mean": _csv(edge_row.forecast_mean),
                    "forecast_sd":   _csv(edge_row.forecast_sd),
                    "yes_ask":       round(edge_row.yes_ask, 4),
                    "yes_bid":       round(bid, 4) if bid == bid else "",
                    "model_prob":    round(edge_row.model_prob, 4),
                    "fee":           round(edge_row.fee, 4),
                    "edge":          round(edge_row.edge, 4),
                    "signal":        signal_tag,
                })
            logger.info("\n".join(rows_log))
            if self._output_dir and csv_rows:
                self._append_csv(
                    self._output_dir / "evaluations.csv", csv_rows,
                    fieldnames=["ts","city","kind","expiry_date","days_out",
                                "ticker","contract",
                                "forecast_p10","forecast_p25","forecast_p50",
                                "forecast_p75","forecast_p90",
                                "forecast_mean","forecast_sd",
                                "yes_ask","yes_bid","model_prob","fee","edge","signal"],
                )

            for edge_row in sorted(edges, key=lambda e: e.edge, reverse=True):
                if edge_row.edge < self._min_edge:
                    continue
                if edge_row.contract not in buy_eligible:
                    continue
                if expiry_counts.get(group_key, 0) >= self._max_per_expiry:
                    break

                ticker = ticker_map.get(edge_row.contract)
                if not ticker:
                    continue

                expiry_counts[group_key] = expiry_counts.get(group_key, 0) + 1
                conviction = min(edge_row.edge / 0.30, 1.0)

                signals.append(Signal(
                    symbol      = ticker,
                    direction   = "long",
                    edge        = round(edge_row.edge, 4),
                    edge_lower  = round(edge_row.edge * 0.5, 4),
                    edge_upper  = round(edge_row.edge * 1.5, 4),
                    horizon     = self._horizon,
                    conviction  = round(conviction, 4),
                    generated_at= snapshot.ts,
                    metadata    = {
                        "city":       city,
                        "kind":       kind,
                        "expiry":     str(expiry_date),
                        "contract":   edge_row.contract,
                        "model_prob": round(edge_row.model_prob, 4),
                        "yes_ask":    round(edge_row.yes_ask, 4),
                        "fee":        round(edge_row.fee, 4),
                        "days_out":   days_out,
                    },
                ))

        return sorted(signals, key=lambda s: s.edge, reverse=True)

    @staticmethod
    def _append_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
        write_header = not path.exists()
        with path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
