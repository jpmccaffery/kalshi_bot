# Integration Handoff: NWS + NBM Forecast Package

## Context

The bot currently fetches temperature forecasts from Open-Meteo and models the
daily high/low as `Normal(forecast_mean, σ = 2 + 1·lead_days)`. Two known
problems with this:

1. Open-Meteo disagrees with NWS data, which is what Kalshi settles against.
2. The fixed-σ normal is overconfident and has the wrong tail shape, which
   misprices both center brackets (B contracts) and tail markets (T contracts).

A new package has been built at `forecast/` that replaces both. It is
self-contained, has no external dependencies beyond `requests`, and ships with
12 passing tests including end-to-end parsing against a real NBM fixture.

**Your job:** integrate this package into the bot, replacing the Open-Meteo
fetch + normal-distribution edge calculation. Do not redesign the package —
it has been designed and tested. Wire it in.

## What's in the package

```
forecast/
├── __init__.py          # public API exports
├── stations.py          # city -> (ICAO, lat, lon, LST tz offset)
├── nws_client.py        # api.weather.gov client (deterministic forecast)
├── nbm_client.py        # NBM NBP text bulletin client + parser (percentiles)
├── distribution.py      # piecewise-linear CDF + bracket probability math
├── recommender.py       # drop-in edge calculator
├── README.md            # usage docs and design rationale
└── tests/test_forecast.py
```

The `Recommender` class is the integration point. Its `score_contracts(...)`
method returns a list of `EdgeRow` objects with the same fields the bot
currently computes: `contract`, `yes_ask`, `model_prob`, `fee`, `edge`.

## Integration steps

### Step 1: Drop the package in

Copy the `forecast/` directory into the bot's source tree. Add `requests` to
dependencies if not already present. Do not modify package internals.

### Step 2: Fill in `stations.py`

Critical. The package ships with 8 cities populated. The bot trades 39.

For each remaining city:

1. Find Kalshi's contract spec PDF for that city's NHIGH / NLOW market on
   their public docs S3 bucket (pattern:
   `https://kalshi-public-docs.s3.amazonaws.com/contract_terms/<symbol>.pdf`).
2. Read the "Underlying" section to confirm the exact NWS station Kalshi
   settles against. **This is not always the major airport** — NYC settles
   on Central Park (KNYC), not LaGuardia or JFK.
3. Look up that station's sensor lat/lon on weather.gov (the airport pages
   list it; for non-airport stations check the NWS station metadata).
4. Confirm Local Standard Time UTC offset (Eastern = -5, Central = -6,
   Mountain = -7, Pacific = -8, Arizona = -7 year-round, etc).
5. Add a `Station(...)` entry to the `STATIONS` dict in `forecast/stations.py`,
   keyed by whatever city string the rest of the bot uses to refer to it.
6. Verify the ICAO appears in NBM's station list at
   https://vlab.noaa.gov/web/mdl/nbm-stations. If it doesn't, find a nearby
   station that does and document the substitution.

The `market_city` field in the dataclass should match exactly the string the
bot's market-event-parser produces for that city. Grep for how cities are
currently keyed in the existing code and use the same convention.

### Step 3: Set the User-Agent strings

In both `forecast/nws_client.py` and `forecast/nbm_client.py`, the
`USER_AGENT` constant is set to a placeholder. Replace with the bot's
identifier and a real contact email. NWS will silently 403 requests
without an identifying User-Agent.

### Step 4: Replace the Open-Meteo fetch and edge calculation

Find where the bot currently:

1. Fetches a forecast from Open-Meteo for a given city / date.
2. Computes a per-contract edge using a normal distribution.

Replace both with a single call to `Recommender.score_contracts(...)`.

The recommender takes:
- `city: str` — must be a key in `STATIONS`
- `target_date: datetime.date` — the LST calendar date the market resolves on
- `kind: str` — `"high"` or `"low"`
- `contracts: list[Contract]` — one per contract for that (city, date, kind)

Build a `Contract` per Kalshi contract from the existing market data:

```python
from forecast import Contract, Recommender

# Once, at bot startup:
recommender = Recommender()

# Per market, per tick:
contracts = []
for market in kalshi_markets_for(city, date, kind):
    if market.label.startswith("B"):
        # 2°F bracket centered on float(label[1:])
        center = float(market.label[1:])
        contracts.append(Contract(
            label=market.label,
            low=center - 1.0,    # adjust if real bracket width differs
            high=center + 1.0,
            yes_ask=market.yes_ask,
        ))
    elif market.label.startswith("T"):
        # T<X (below lowest B): low=None, high=X
        # T>X (above highest B): low=X, high=None
        # The current bot already determines this from strike position
        # within the event; preserve that logic to fill low/high here.
        ...

edges = recommender.score_contracts(city, date, kind, contracts)
# edges is a list of EdgeRow with .contract, .yes_ask, .model_prob, .fee, .edge
```

### Step 5: Verify bracket geometry matches reality

The summary said B brackets are 2°F wide centered at half-degree values
(e.g. B88.5 = 88-89°F). When wiring up `Contract.low` and `Contract.high`,
sanity-check this against actual Kalshi market data for one event. The
package math integrates probability over `[low, high]`, so getting the
bracket bounds right matters for the edge calc to be correct.

If bracket geometry varies between events (some 1°F wide, some 2°F), pass
the actual `low`/`high` from the market data rather than computing them
from the label.

### Step 6: Keep the existing signal logic

The recommender outputs the same fields the existing bot consumes:
`model_prob`, `fee`, `edge`. Do **not** change downstream signal thresholds
or sizing as part of this integration — those are separate concerns
addressed in a later phase. The point of this change is just to make
`model_prob` reflect reality. After running with the new probabilities for
a couple weeks, signal thresholds can be retuned based on the new
distribution of edges.

### Step 7: Logging

The recommender already logs at INFO level when NWS deterministic forecast
disagrees with NBM p50 by more than 3°F. These are signal events worth
keeping. Make sure the bot's log config doesn't suppress INFO from the
`forecast.*` loggers.

Add a debug log line at the bot level for each `score_contracts` call that
captures: city, target_date, kind, num_contracts, source_cycle (from the
distribution metadata), top_edge_contract, top_edge_value. Useful for
shadow-mode analysis.

### Step 8: Run the package's own tests in CI

`forecast/tests/test_forecast.py` is a self-contained test module. It can
be run with `python forecast/tests/test_forecast.py` (no pytest required,
though pytest works too). Add it to whatever test runner the bot uses.
All 12 tests should pass.

### Step 9: Smoke-test against live data before paper trading

Write a one-off script that, for each city in `STATIONS`:
1. Fetches today's NBP percentiles for both high and low.
2. Fetches today's NWS deterministic forecast for high and low.
3. Prints them side by side.

Run it once. Verify:
- All 39 cities return data without errors.
- NWS and NBM p50 are within a few degrees of each other for most cities.
- No city is missing from NBM's station list.
- LST date alignment looks right (the high "for today" should correspond
  to the daytime period of today's local calendar date).

Once that script runs clean, the bot is ready to paper-trade with the new
recommender.

## What NOT to do

- Don't modify `forecast/distribution.py` to use a normal distribution
  "as a fallback" without thinking. The piecewise-linear CDF from
  percentiles is the model. There's a `normal_blend_weight` knob on
  `Recommender(...)` for mixing in some normal mass (default 0), but
  don't turn it on without backtest evidence.
- Don't change the fee formula. `taker_fee = 0.07 × yes_ask × (1 - yes_ask)`
  is correct and matches Kalshi's published formula.
- Don't fetch forecasts from anywhere else and "blend" with NBM. The whole
  point is to use the data Kalshi settles against. If you want to add
  another source later (e.g. ECMWF ensemble), do it as a separate phase
  with proper validation.
- Don't change the stop-loss / take-profit / sizing logic in this PR.
  Those are known issues but they're addressed in a separate phase. Keep
  this change scoped to forecast quality.
- Don't fall back to Open-Meteo on errors. If NBM is unavailable for a
  city/date, log it and skip the market for that tick. Trading on bad data
  is worse than not trading.

## Behavioral changes the bot operator should expect

- **Tail contract probabilities will generally drop.** The old normal-
  distribution model invented fat tails. NBM's empirically-calibrated
  percentiles plus slope-extrapolation produce shorter, more realistic
  tails. T contracts that previously triggered buy signals may stop
  triggering. This is correct.
- **Center bracket probabilities will sharpen on stable forecasts and
  widen on uncertain ones.** Heteroskedasticity is the point. A summer
  ridge day might show p10..p90 spanning only 4°F; a winter front day
  might span 12°F. Bracket probabilities will reflect that.
- **Fewer signals on stale data.** NBP fully populated cycles are at
  01Z/07Z/13Z/19Z. Between cycles, the distribution is the same as it
  was at the last cycle. Don't re-trigger the same buy across multiple
  ticks within a single cycle window — the existing "1 signal per
  expiry per tick" cap probably handles this, but verify.
- **Some cities may produce no data.** If a station's NBP entry is
  incomplete, the recommender returns an empty list rather than
  fabricating numbers. Log this and move on.

## Files to reference if you need more detail

- `forecast/README.md` — the package's own design doc, written for a
  human reader. Has a comparison table of old vs new and a section on
  what to watch in production.
- `forecast/nbm_client.py` — top-of-file docstring explains NBP cycle
  timing, date alignment with Kalshi, and the bulletin URL pattern.
- `forecast/distribution.py` — top-of-file docstring explains why
  piecewise-linear-from-percentiles is the right model and why a normal
  is wrong.

## Out of scope for this PR

These are deliberately deferred and should NOT be added now:

- Per-station rolling bias correction
- Late-day Bayesian update from METAR observations
- Fractional Kelly sizing
- Probability-based stop loss replacement
- Calibration measurement and CRPS tracking

Each is its own phase with its own validation. Keep this PR focused on
"replace the forecast and the distribution," nothing else.
