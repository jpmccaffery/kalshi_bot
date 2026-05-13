# Kalshi Temperature Market Bot — Strategy Summary

We trade Kalshi temperature prediction markets across 39 US cities. Each market is a binary contract that resolves YES based on whether the daily high or low temperature falls in a specific range.

## Market structure

- `B{x}` contracts resolve YES if the temperature falls in a 2°F bracket centered around x (e.g. B88.5 = between 88-89°F)
- `T{x}` contracts are tail markets — either below the lowest bracket or above the highest bracket, identified by comparing strike positions within the event

## Edge computation

We fetch a temperature forecast (currently Open-Meteo, which has accuracy issues vs the NWS data Kalshi settles against) and model the temperature as a normal distribution with σ = 2°F + 1°F per day out. For each contract we compute:

- `model_prob` = probability of YES under our normal distribution
- `taker_fee` = `0.07 × yes_ask × (1 - yes_ask)` (Kalshi's fee formula)
- `edge` = `model_prob - yes_ask - taker_fee`

We signal a buy when `edge ≥ 0.10` and `yes_ask ≥ 0.10`, capped at 1 signal per expiry date per tick.

## Execution

Positions are sized at a fixed $100 per trade. We exit via stop loss (-10%), take profit (+10%), or time limit (6 hours). We're currently paper trading against live prices with a $10,000 virtual balance.

## Known issues

- Forecast data (Open-Meteo) can disagree significantly with NWS data that Kalshi settles against
- The bid-ask spread on many contracts exceeds our 10% stop loss, causing immediate exits
- σ = 2°F is likely too tight, making the model overconfident
