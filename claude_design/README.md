# Kalshi Weather Forecast Package — Steps 1 & 2

Replaces Open-Meteo + N(mean, 2+lead) with NWS official forecasts and NBM
calibrated percentile distributions.

## Files

```
forecast/
├── __init__.py          # public API
├── stations.py          # city -> (ICAO, lat, lon, tz) mapping
├── nws_client.py        # api.weather.gov client
├── nbm_client.py        # NBP probabilistic text bulletin client + parser
├── distribution.py      # piecewise-linear CDF + bracket math
├── recommender.py       # drop-in for the edge calculation
└── tests/
    └── test_forecast.py # 12 tests, including end-to-end on real NBP fixture
```

## Quick start

```python
import datetime as dt
from forecast import Recommender, Contract

rec = Recommender()
contracts = [
    Contract("B86.5", low=86, high=87, yes_ask=0.18),
    Contract("B88.5", low=88, high=89, yes_ask=0.25),
    Contract("B90.5", low=90, high=91, yes_ask=0.20),
    Contract("T<84",  low=None, high=84, yes_ask=0.05),
    Contract("T>92",  low=92,  high=None, yes_ask=0.07),
]
edges = rec.score_contracts("new_york", dt.date.today(), "high", contracts)
for e in edges:
    print(e)
```

`score_contracts` returns the same fields you already use:
`contract`, `yes_ask`, `model_prob`, `fee`, `edge`. Drop them straight into
your existing signal logic.

## Two things to verify before going live

1. **Fill in the rest of the cities in `stations.py`.** I've put 8 of your
   39 with high confidence (NYC=KNYC=Central Park is the most important —
   note Kalshi NHIGH explicitly settles against Central Park, NOT LGA/JFK).
   For each remaining city, find Kalshi's contract spec PDF and confirm
   the settlement station's ICAO code, then look up its sensor lat/lon
   on weather.gov.

2. **Set `USER_AGENT` in both clients to your actual contact info.** NWS
   requires this; NOMADS doesn't strictly require it but it's good
   citizenship.

## What changed vs your old code

| | Old | New |
|--|-----|-----|
| Forecast source | Open-Meteo (third-party blend) | NWS api.weather.gov + NBM NBP |
| Distribution | Normal, σ = 2 + 1·lead_days | Piecewise-linear CDF from NBP percentiles |
| Calibration | None | NOAA does it for us (rolling 120-day quantile mapping) |
| Tail markets | Pure normal-tail | Slope-extrapolated from p10/p25 and p75/p90 |
| Per-station bias | None | Implicit in NBM (it's URMA-anchored), explicit in Step 3 (later) |
| Sanity check | None | NWS official forecast vs NBM p50, logs warnings on disagreement >3°F |

## Important behavioral notes

### Conservative tails

The piecewise-linear CDF extrapolates beyond the published p10/p90 using the
slope of the inner segments, capped to a sensible support range. This means
extreme tail temperatures (e.g. asking the probability of 105°F when p90 is
93°F) often return zero or very small numbers. **This is intentional.** The
old normal-distribution model would invent fat tails and likely overpriced
mass there. If you find on real data that NBP underprices tails too aggressively
for your taste, set `Recommender(normal_blend_weight=0.05)` to mix in 5%
weight from a normal fitted to the NBP mean and SD.

### Date alignment

NWS settles on the **Daily Climate Report** which uses **Local Standard Time**
year-round, even during DST. The package handles this internally:
`station.tz_standard_offset` is always the LST offset (e.g. -5 for Eastern,
not -4 during DST). If your bot already deals in LST dates, you're fine. If
it uses local-time-with-DST, normalize before calling.

### NBP cycle availability

Full NBP data is only available at 01Z, 07Z, 13Z, 19Z (every 6 hours). The
client automatically picks the most recent fully-populated cycle, with a
2-hour buffer for upload latency. So in steady state you get a fresh
distribution every 6 hours. That's fine for your "minutes" latency target.

### What to watch in production

- Log warnings when NWS official forecast disagrees with NBM p50 by >3°F.
  These are cases where NWS forecasters manually overrode the blend and
  often have local knowledge worth respecting. (Future enhancement: weight
  the NWS deterministic into the distribution.)
- Watch for stations with persistent disagreement; that's a calibration
  issue to fix in Step 3.
- Track CRPS (continuous ranked probability score) on (forecast, observed)
  pairs over time. That's the right metric for "is my distribution good?"

## Tests

```
$ python forecast/tests/test_forecast.py
  PASS  test_cdf_monotone_and_bounded
  PASS  test_cdf_hits_known_percentiles
  PASS  test_bracket_prob_around_median
  PASS  test_skewed_distribution
  PASS  test_extreme_tail_extrapolation_is_capped
  PASS  test_bracket_helpers
  PASS  test_degenerate_percentiles_dont_crash
  PASS  test_extract_station_block_simple
  PASS  test_parse_columns_handles_pipes_and_negatives
  PASS  test_parse_nbp_block_full
  PASS  test_parse_nbp_skips_missing_values
  PASS  test_full_pipeline_from_fixture
12/12 passed
```

The end-to-end test parses a real NBP fixture from the NOAA v4.1 docs and
verifies the full pipeline: bulletin -> parsed percentiles -> CDF ->
bracket probability sums to 1.0 across the full support.

## Step 3 preview

Once you've run this in shadow mode for a couple weeks and have logged
(forecast, observed) pairs, the next steps are:

- Per-station bias correction (subtract rolling-30-day mean error from p50,
  recenter the whole CDF). Often worth 0.5–1°F MAE per station.
- Calibration check: bin by published percentile and verify the empirical
  hit rate matches. Adjust the CDF if not.
- Late-day Bayesian update for daily-high markets using current METAR
  observations. Probably your single biggest edge against retail.
