"""
Tests for the forecast package.

The most failure-prone piece is parsing the NBP text bulletin. We test it
against the actual fixture from the NOAA NBM v4.1 documentation page so we
catch any regression against the live format.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kalshi_bot.forecast.distribution import (from_percentiles, bracket_prob_B,
                                   bracket_prob_T_below, bracket_prob_T_above)
from kalshi_bot.forecast.nbm_client import (_extract_station_block, _parse_nbp_block,
                                 _parse_columns)


# ---- distribution math ----

def test_cdf_monotone_and_bounded():
    d = from_percentiles(p10=50, p25=55, p50=60, p75=65, p90=70)
    xs = [30, 40, 50, 55, 60, 65, 70, 80, 90]
    cs = [d.cdf(x) for x in xs]
    assert cs[0] == 0.0
    assert cs[-1] == 1.0
    for a, b in zip(cs, cs[1:]):
        assert b >= a - 1e-9, f"CDF not monotone: {cs}"


def test_cdf_hits_known_percentiles():
    d = from_percentiles(p10=50, p25=55, p50=60, p75=65, p90=70)
    assert abs(d.cdf(50) - 0.10) < 1e-6
    assert abs(d.cdf(55) - 0.25) < 1e-6
    assert abs(d.cdf(60) - 0.50) < 1e-6
    assert abs(d.cdf(65) - 0.75) < 1e-6
    assert abs(d.cdf(70) - 0.90) < 1e-6


def test_bracket_prob_around_median():
    # Tight distribution around 60: p10..p90 spans 56..64
    d = from_percentiles(p10=56, p25=58, p50=60, p75=62, p90=64)
    p_60_61 = d.prob_in_range(60, 61)
    # Should be sizable but less than 25% (which is 60..62 range)
    assert 0.10 < p_60_61 < 0.20


def test_skewed_distribution():
    # Right-skewed: median 60 but long warm tail
    d = from_percentiles(p10=58, p25=59, p50=60, p75=63, p90=70)
    # Mass below median should be more concentrated than above.
    p_below_61 = d.prob_below(61)
    p_above_61 = d.prob_above(61)
    # Doesn't have to be a particular split, but should respect the asymmetry:
    # the warm tail extends much further, so the cool side is denser.
    # 58->60 covers 40% of mass over 2F; 60->70 covers 40% of mass over 10F.
    assert p_below_61 > 0.55  # most mass on the cool/center side at 61
    assert p_above_61 < 0.45


def test_extreme_tail_extrapolation_is_capped():
    d = from_percentiles(p10=50, p25=55, p50=60, p75=65, p90=70)
    # Way beyond the implied support: zero.
    assert d.prob_below(20) == 0.0
    assert d.prob_above(120) == 0.0
    # AT the published 10/90 percentiles we hit exactly 0.10 / 0.10.
    assert abs(d.prob_below(50) - 0.10) < 1e-6
    assert abs(d.prob_above(70) - 0.10) < 1e-6
    # Slightly inside the extrapolated tail.
    assert 0.0 < d.prob_below(48) < 0.10
    assert 0.0 < d.prob_above(72) < 0.10


def test_bracket_helpers():
    d = from_percentiles(p10=85, p25=87, p50=89, p75=91, p90=93)
    # B89 covers [88,90]; symmetric about p50=89 with p25..p75 = [87,91],
    # so [88,90] is roughly the inner half of the IQR -> ~0.25.
    p_b = bracket_prob_B(d, center=89, width=2)
    assert 0.20 < p_b < 0.30
    # T below 84 is below the lower extrapolated tail bound (~83.7).
    p_below = bracket_prob_T_below(d, threshold=84)
    assert 0.0 <= p_below < 0.05
    # T above 95 is above the upper extrapolated tail bound (~94.3).
    p_above = bracket_prob_T_above(d, threshold=95)
    assert p_above == 0.0


def test_degenerate_percentiles_dont_crash():
    # All percentiles equal (extremely confident forecast).
    d = from_percentiles(p10=60, p25=60, p50=60, p75=60, p90=60)
    # CDF should still be defined everywhere; bracket containing 60 -> ~1.0
    p = d.prob_in_range(59, 61)
    assert 0.95 <= p <= 1.0


# ---- NBP parsing ----

# Fixture from https://vlab.noaa.gov/web/mdl/nbm-textcard-v4.1, KBWI 13Z 2022-05-01
NBP_FIXTURE = """\
KBWI   NBM V4.1 NBP GUIDANCE  5/01/2022  1300 UTC
       MON 02| TUE 03| WED 04| THU 05| FRI 06| SAT 07| SUN 08| MON 09| TUE 10|
UTC    12| 00 12| 00 12| 00 12| 00 12| 00 12| 00 12| 00 12| 00 12| 00
FHR    23| 35 47| 59 71| 83 95|107 119|131 143|155 167|179 191|203 215|227
TXNMN  55| 77 52| 75 55| 79 55| 74 54| 70 52| 65 48| 67 49| 71 53| 77
TXNSD   2|  3  2|  5  4|  6  3|  5  4|  6  4|  7  5|  6  5|  7  5|  7
TXNP1  53| 74 50| 69 50| 71 51| 69 49| 61 46| 55 42| 59 41| 63 46| 68
TXNP2  54| 76 51| 73 52| 74 53| 71 51| 66 48| 59 44| 62 45| 66 48| 72
TXNP5  55| 77 52| 76 55| 80 55| 74 54| 71 51| 65 47| 67 48| 70 52| 76
TXNP7  56| 79 54| 78 58| 84 56| 77 56| 74 54| 70 51| 71 53| 75 56| 81
TXNP9  58| 81 55| 80 61| 86 58| 80 59| 78 58| 74 54| 75 56| 80 60| 87
"""


def test_extract_station_block_simple():
    block = _extract_station_block(NBP_FIXTURE, "KBWI")
    assert block is not None
    assert "TXNP5" in block
    assert _extract_station_block(NBP_FIXTURE, "KZZZ") is None


def test_parse_columns_handles_pipes_and_negatives():
    # Real NBP line has alternating `value |` and `| value`; both should split fine
    name, toks = _parse_columns("TXNP5  55| 77 52| 76 55|-99 55| 74")
    assert name == "TXNP5"
    assert toks == ["55", "77", "52", "76", "55", "-99", "55", "74"]


def test_parse_nbp_block_full():
    block = _extract_station_block(NBP_FIXTURE, "KBWI")
    cycle_dt = dt.datetime(2022, 5, 1, 13, tzinfo=dt.timezone.utc)
    pcts = _parse_nbp_block(block, "KBWI", cycle_dt, station_tz_offset=-5)
    # We expect entries for both 12Z (lows) and 00Z (highs) columns.
    highs = [p for p in pcts if p.kind == "high"]
    lows = [p for p in pcts if p.kind == "low"]
    assert len(highs) >= 8
    assert len(lows) >= 8

    # Spot-check the first MAX column. UTC=00, FHR=35, valid 2022-05-03 00Z,
    # which is daytime high of LST date 2022-05-02 (Monday May 2).
    first_high = highs[0]
    assert first_high.target_date == dt.date(2022, 5, 2)
    assert first_high.p50 == 77.0
    assert first_high.p10 == 74.0
    assert first_high.p90 == 81.0
    assert first_high.mean == 77.0
    assert first_high.sd == 3.0

    # Spot-check the first MIN column. UTC=12, FHR=23, valid 2022-05-02 12Z,
    # which is the overnight low ending the morning of LST May 2.
    first_low = lows[0]
    assert first_low.target_date == dt.date(2022, 5, 2)
    assert first_low.p50 == 55.0
    assert first_low.p10 == 53.0
    assert first_low.p90 == 58.0


def test_parse_nbp_skips_missing_values():
    # Fixture with -99 in a temperature row.
    bad = NBP_FIXTURE.replace("TXNP5  55| 77",
                              "TXNP5 -99| 77")
    block = _extract_station_block(bad, "KBWI")
    cycle_dt = dt.datetime(2022, 5, 1, 13, tzinfo=dt.timezone.utc)
    pcts = _parse_nbp_block(block, "KBWI", cycle_dt, station_tz_offset=-5)
    lows = [p for p in pcts if p.kind == "low"]
    # First low's p50 was -99 -> should be None and is_complete() False.
    assert lows[0].p50 is None
    assert lows[0].is_complete() is False


def test_full_pipeline_from_fixture():
    """End-to-end: parse fixture, build distribution, compute brackets."""
    block = _extract_station_block(NBP_FIXTURE, "KBWI")
    cycle_dt = dt.datetime(2022, 5, 1, 13, tzinfo=dt.timezone.utc)
    pcts = _parse_nbp_block(block, "KBWI", cycle_dt, station_tz_offset=-5)
    high_may2 = next(p for p in pcts
                     if p.kind == "high" and p.target_date == dt.date(2022, 5, 2))
    d = from_percentiles(high_may2.p10, high_may2.p25, high_may2.p50,
                         high_may2.p75, high_may2.p90,
                         mean=high_may2.mean, sd=high_may2.sd)
    # Sanity: cumulative probs at each percentile.
    assert abs(d.cdf(74) - 0.10) < 1e-6
    assert abs(d.cdf(81) - 0.90) < 1e-6
    # Brackets sum to ~1 across the full span we care about.
    total = (d.prob_below(70)
             + d.prob_in_range(70, 72) + d.prob_in_range(72, 74)
             + d.prob_in_range(74, 76) + d.prob_in_range(76, 78)
             + d.prob_in_range(78, 80) + d.prob_in_range(80, 82)
             + d.prob_in_range(82, 84) + d.prob_above(84))
    assert abs(total - 1.0) < 1e-6


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f" ERROR  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(0 if failed == 0 else 1)
