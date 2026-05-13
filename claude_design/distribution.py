"""
Build a forecast distribution from NBP percentiles, then compute the
probability mass in arbitrary temperature brackets (Kalshi `B{x}` and `T{x}`).

WHY NOT JUST USE A NORMAL?
-------------------------
Your old model used N(mean, sigma=2+lead_days). That's wrong in three ways:

  1. Real forecast errors are heteroskedastic: a stable summer high pressure
     ridge has sigma < 1F at a 1-day lead, while an active winter pattern
     can have sigma > 5F at the same lead. NBM publishes the right sigma
     per (station, day, regime).
  2. Errors are often skewed (cold-air-damming, valley cold pools, marine
     layer breakdown). NBP gives us asymmetric percentiles for free.
  3. Tails are fatter than normal, especially for min temps. Pricing
     `T{x}` (tail) contracts off a normal will systematically underprice
     extreme outcomes.

WHAT WE BUILD
-------------
A piecewise-linear CDF anchored at the 5 published percentiles, with
linear extrapolation in the tails using the local slope. Then for any
[a, b] bracket, P(temp in [a,b]) = F(b) - F(a).

WHY PIECEWISE LINEAR?
---------------------
Kalshi brackets are 2 F wide (B88.5 = [88,89]). Inside a 2 F window,
linear interpolation of the CDF is essentially as good as any fancier
fit, and it has no shape assumptions to be wrong about. We don't need
a parametric distribution; we need bracket probabilities.

For tails beyond the 10th/90th percentile, we extrapolate using the
local slope of the CDF, but we cap probability mass to fall to zero
linearly over a "tail width" set from the percentile spread. This is a
mild assumption that prevents nonsense like negative probabilities and
keeps the tail probabilities sane.

We also expose a "blend" option that mixes the NBP CDF with a small
amount of normal-distribution mass scaled by the NBP standard deviation.
This is purely a robustness backstop for cases where NBP percentiles are
clearly broken (e.g. p10 == p90, which happens occasionally for very
stable outlooks). Default weight is 0; turn it up only if your
backtests say to.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class ForecastDistribution:
    """Piecewise-linear CDF defined by anchor (temperature, cumulative_prob) pairs."""
    anchors: list[tuple[float, float]]
    # Optional normal blend, used as a fallback. weight in [0,1].
    normal_mean: Optional[float] = None
    normal_sd: Optional[float] = None
    normal_weight: float = 0.0

    def cdf(self, x: float) -> float:
        """P(T <= x) under this distribution."""
        c_pwl = _cdf_piecewise(self.anchors, x)
        if self.normal_weight > 0 and self.normal_mean is not None and self.normal_sd:
            c_norm = _normal_cdf(x, self.normal_mean, self.normal_sd)
            return (1 - self.normal_weight) * c_pwl + self.normal_weight * c_norm
        return c_pwl

    def prob_in_range(self, low: float, high: float) -> float:
        """P(low <= T <= high)."""
        if high <= low:
            return 0.0
        return max(0.0, min(1.0, self.cdf(high) - self.cdf(low)))

    def prob_below(self, x: float) -> float:
        return self.cdf(x)

    def prob_above(self, x: float) -> float:
        return 1.0 - self.cdf(x)


def from_percentiles(p10: float, p25: float, p50: float, p75: float, p90: float,
                     mean: Optional[float] = None,
                     sd: Optional[float] = None,
                     normal_weight: float = 0.0
                     ) -> ForecastDistribution:
    """
    Build a piecewise-linear CDF from NBP percentile values.

    We add tail anchors at p=0 and p=1, extrapolated from the slope
    of the inner two segments. This gives well-defined probabilities
    for any temperature value, while keeping the inner brackets exactly
    consistent with the published percentiles.
    """
    # Enforce monotonicity. NBP is generally monotone but tiny numerical
    # quirks can violate it; nudge any non-monotone point.
    pts = [p10, p25, p50, p75, p90]
    for i in range(1, len(pts)):
        if pts[i] < pts[i-1]:
            pts[i] = pts[i-1]
    p10, p25, p50, p75, p90 = pts

    # Tail extrapolation: assume the same slope as the nearest interior
    # segment, but bounded so the implied tail isn't ridiculous.
    # Lower tail: from (p10, 0.10) and (p25, 0.25), extrapolate to 0.
    if p25 > p10:
        slope_low = (0.25 - 0.10) / (p25 - p10)        # prob per degree F
        # Distance (in F) from p10 down to where prob = 0:
        delta_low = 0.10 / max(slope_low, 1e-9)
    else:
        delta_low = 1.0
    delta_low = max(0.5, min(delta_low, 25.0))         # safety bounds
    p_min_temp = p10 - delta_low

    if p90 > p75:
        slope_hi = (0.90 - 0.75) / (p90 - p75)
        delta_hi = 0.10 / max(slope_hi, 1e-9)
    else:
        delta_hi = 1.0
    delta_hi = max(0.5, min(delta_hi, 25.0))
    p_max_temp = p90 + delta_hi

    anchors = [
        (p_min_temp, 0.0),
        (p10, 0.10),
        (p25, 0.25),
        (p50, 0.50),
        (p75, 0.75),
        (p90, 0.90),
        (p_max_temp, 1.0),
    ]
    # Deduplicate equal-x anchors (rare, but possible if percentiles collapse).
    deduped: list[tuple[float, float]] = []
    for x, p in anchors:
        if deduped and x <= deduped[-1][0]:
            x = deduped[-1][0] + 1e-6
        deduped.append((x, p))

    return ForecastDistribution(
        anchors=deduped,
        normal_mean=mean,
        normal_sd=sd,
        normal_weight=normal_weight,
    )


def _cdf_piecewise(anchors: list[tuple[float, float]], x: float) -> float:
    """Linear interp on (temperature, cumulative_prob) anchors."""
    if x <= anchors[0][0]:
        return 0.0
    if x >= anchors[-1][0]:
        return 1.0
    # Binary search would be faster; for 7 anchors a linear scan is fine.
    for i in range(1, len(anchors)):
        x0, p0 = anchors[i-1]
        x1, p1 = anchors[i]
        if x <= x1:
            t = (x - x0) / (x1 - x0)
            return p0 + t * (p1 - p0)
    return 1.0


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


# ---------- Kalshi-specific bracket helpers ----------

@dataclass
class BracketProb:
    contract: str          # "B88.5" or "T<60" / "T>95"
    low: Optional[float]   # None for one-sided tails
    high: Optional[float]
    prob: float


def bracket_prob_B(dist: ForecastDistribution, center: float,
                   width: float = 2.0) -> float:
    """
    Probability that the daily temperature falls in a B{x} bracket.
    `center` is the bracket center (e.g. 88.5 for the 88-89 bracket).
    Kalshi B brackets are [floor(center), floor(center)+1] for half-degree
    centers. Default width=2 matches your existing convention; verify
    against your actual market structure before going live.
    """
    low = center - width / 2.0
    high = center + width / 2.0
    return dist.prob_in_range(low, high)


def bracket_prob_T_below(dist: ForecastDistribution, threshold: float) -> float:
    """T contract: temperature strictly below the lowest B bracket."""
    return dist.prob_below(threshold)


def bracket_prob_T_above(dist: ForecastDistribution, threshold: float) -> float:
    """T contract: temperature strictly above the highest B bracket."""
    return dist.prob_above(threshold)
