"""
Drop-in replacement for the edge calculation in your bot.

INPUT:
    - city (must be in stations.STATIONS)
    - target_date (LST calendar date the market resolves on)
    - kind: "high" or "low"
    - list of (contract_label, low, high, yes_ask) tuples for that market

OUTPUT:
    - list of EdgeRow dicts you can feed straight into your signal logic.

CHANGES VS YOUR CURRENT BOT:
    - model_prob now comes from NBP percentile CDF, not N(mean, 2+lead).
    - Uses NWS forecast as a sanity-check (logs a warning if the NWS
      official forecast disagrees with NBM p50 by > 3F; that's a flag
      that NWS forecasters manually adjusted the blend).
    - Fee formula and edge formula are unchanged.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, asdict
from typing import Optional

from .distribution import (ForecastDistribution, from_percentiles,
                           bracket_prob_B, bracket_prob_T_below,
                           bracket_prob_T_above)
from .nbm_client import NBMClient, TempPercentiles
from .nws_client import NWSClient
from .stations import get_station, Station

log = logging.getLogger(__name__)

# Kalshi taker fee.
TAKER_FEE_RATE = 0.07


@dataclass
class EdgeRow:
    contract: str
    yes_ask: float
    model_prob: float
    fee: float
    edge: float
    # NO-side fields
    no_ask: float = 0.0
    no_fee: float = 0.0
    no_edge: float = float("-inf")
    forecast_p10: Optional[float] = None
    forecast_p25: Optional[float] = None
    forecast_p50: Optional[float] = None
    forecast_p75: Optional[float] = None
    forecast_p90: Optional[float] = None
    forecast_mean: Optional[float] = None
    forecast_sd: Optional[float] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class Contract:
    """One Kalshi contract on a single (city, date, kind) market."""
    label: str            # "B88.5", "T<60", "T>95"
    low: Optional[float]  # None for one-sided tails
    high: Optional[float]
    yes_ask: float
    no_ask: float = 0.0


def taker_fee(yes_ask: float) -> float:
    return TAKER_FEE_RATE * yes_ask * (1.0 - yes_ask)


class Recommender:
    def __init__(self,
                 nws: Optional[NWSClient] = None,
                 nbm: Optional[NBMClient] = None,
                 nws_disagreement_warn_f: float = 3.0,
                 normal_blend_weight: float = 0.0):
        self._nws = nws or NWSClient()
        self._nbm = nbm or NBMClient()
        self._nws_warn = nws_disagreement_warn_f
        self._blend = normal_blend_weight

    def get_distribution(self, city: str, target_date: dt.date, kind: str
                         ) -> Optional[ForecastDistribution]:
        """Build the forecast distribution for one (city, date, kind)."""
        st = get_station(city)
        if st is None:
            log.warning("Unknown city: %s", city)
            return None

        # Pull NBP percentiles. One bulletin covers the whole 9-day range.
        try:
            all_pcts = self._nbm.get_percentiles(
                st.icao, station_tz_offset=st.tz_standard_offset)
        except Exception as e:
            log.error("NBP fetch failed for %s: %s", st.icao, e)
            return None

        match = next((p for p in all_pcts
                      if p.target_date == target_date and p.kind == kind), None)
        if match is None:
            log.warning("No NBP %s data for %s on %s (not in bulletin)",
                        kind, st.icao, target_date)
            return None
        if match.is_sentinel():
            log.debug("NBP %s percentiles for %s on %s are sentinel — "
                      "event near-expiry, holding to settlement",
                      kind, st.icao, target_date)
            return None
        if not match.is_complete():
            log.warning("Partial NBP %s percentiles for %s on %s (fhr=%d)",
                        kind, st.icao, target_date, match.fhr)
            return None

        dist = from_percentiles(
            p10=match.p10, p25=match.p25, p50=match.p50,
            p75=match.p75, p90=match.p90,
            mean=match.mean, sd=match.sd,
            normal_weight=self._blend,
        )

        # Sanity check against NWS official forecast.
        try:
            nws_fcsts = self._nws.get_daily_forecasts(
                st.lat, st.lon, st.tz_standard_offset)
            nws_match = next((f for f in nws_fcsts if f.date == target_date), None)
            if nws_match is not None:
                nws_val = nws_match.high_f if kind == "high" else nws_match.low_f
                if nws_val is not None and abs(nws_val - match.p50) > self._nws_warn:
                    log.info("NWS-NBM disagreement at %s %s %s: NWS=%.1f NBM_p50=%.1f",
                             st.icao, target_date, kind, nws_val, match.p50)
        except Exception as e:
            log.debug("NWS sanity check skipped: %s", e)

        return dist

    def score_contracts(self, city: str, target_date: dt.date, kind: str,
                        contracts: list[Contract]) -> list[EdgeRow]:
        dist = self.get_distribution(city, target_date, kind)
        if dist is None:
            return []
        rows: list[EdgeRow] = []
        for c in contracts:
            p      = self._contract_prob(dist, c)
            fee    = taker_fee(c.yes_ask)
            edge   = p - c.yes_ask - fee
            no_prob = 1.0 - p
            no_fee  = taker_fee(c.no_ask) if c.no_ask > 0 else 0.0
            no_edge = no_prob - c.no_ask - no_fee if c.no_ask > 0 else float("-inf")
            rows.append(EdgeRow(
                contract=c.label, yes_ask=c.yes_ask,
                model_prob=p, fee=fee, edge=edge,
                no_ask=c.no_ask, no_fee=no_fee, no_edge=no_edge,
                forecast_p10=dist.p10,
                forecast_p25=dist.p25,
                forecast_p50=dist.p50,
                forecast_p75=dist.p75,
                forecast_p90=dist.p90,
                forecast_mean=dist.normal_mean,
                forecast_sd=dist.normal_sd,
            ))
        return rows

    @staticmethod
    def _contract_prob(dist: ForecastDistribution, c: Contract) -> float:
        if c.label.startswith("T"):
            if c.low is None and c.high is not None:
                return bracket_prob_T_below(dist, c.high)
            if c.high is None and c.low is not None:
                return bracket_prob_T_above(dist, c.low)
            # Two-sided "T" shouldn't happen, but handle it.
            return dist.prob_in_range(c.low or -200, c.high or 200)
        # B contract
        if c.low is not None and c.high is not None:
            return dist.prob_in_range(c.low, c.high)
        # Fall back to using label center and 2 F width
        try:
            center = float(c.label[1:])
            return bracket_prob_B(dist, center)
        except ValueError:
            return 0.0
