"""
kalshi_bot sell engine extensions.

ModelBasedSellEngine
    Sells a position when the market's current yes_bid (net of taker fee)
    exceeds the model's probability for that contract — i.e., the market is
    now offering more than holding to settlement is worth.

CompositeSellEngine
    Wraps two or more sell engines and marks a position for sale if *any*
    of the constituent engines says to sell it.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from kalshi_bot.forecast.recommender import taker_fee
from trading_bot.sell_engine import SellDecisionProtocol

if TYPE_CHECKING:
    from kalshi_bot.temp_recommender import TemperatureRecommender

logger = logging.getLogger(__name__)


class ModelBasedSellEngine:
    """
    Exit a position when the market bid (net of taker fee) exceeds our model
    probability. The logic: if yes_bid - fee > model_prob, selling now yields
    more than the expected value of holding to settlement.

    Requires TemperatureRecommender to have been called this tick so that
    _model_probs is populated.
    """

    def __init__(self, recommender: "TemperatureRecommender",
                 output_dir: Path | None = None) -> None:
        self._recommender = recommender
        self._output_dir  = Path(output_dir) if output_dir else None
        # First bid seen per ticker — used to compute entry spread.
        self._entry_bids: dict[str, float] = {}

    @property
    def post_evaluate(self) -> Callable[[pd.DataFrame, list], None] | None:
        return None

    def evaluate(
        self,
        positions: pd.DataFrame,
        signals:   list,
        prices:    pd.DataFrame,
    ) -> pd.DataFrame:
        if positions.empty:
            return pd.DataFrame()
        if prices.empty:
            logger.warning("ModelSell: prices empty — cannot evaluate")
            return pd.DataFrame()

        price_map: dict[str, pd.Series] = {}
        if "symbol" in prices.columns:
            for _, row in prices.iterrows():
                price_map[row["symbol"]] = row

        rows_to_sell = []
        table = [
            f"{'TICKER':<42} {'entry':>6} {'ask':>6} {'bid':>6} "
            f"{'model':>6} {'net_sell':>8} {'pnl%':>6}  decision"
        ]
        for _, pos in positions.iterrows():
            symbol      = pos.get("symbol")
            entry_price = float(pos.get("avg_entry_price", 0) or 0)
            if not symbol:
                continue

            model_prob  = self._recommender.get_model_prob(symbol)
            kalshi_side = self._recommender.get_position_side(symbol)
            price_row   = price_map.get(symbol)

            if model_prob is None or price_row is None:
                table.append(f"  {symbol:<40}  {'?':>6}  (no model/price data this tick — holding)")
                continue

            yes_ask = float(price_row.get("yes_ask", float("nan")) or float("nan"))

            if kalshi_side == "no":
                no_bid   = float(price_row.get("no_bid", 0) or 0)
                no_prob  = 1.0 - model_prob
                if no_bid <= 0 or no_bid != no_bid:
                    table.append(
                        f"  {symbol:<40}  entry={entry_price:.3f}"
                        f"  no_model={no_prob:.3f}  no_bid=n/a  → no bid, holding [NO]"
                    )
                    continue
                fee      = taker_fee(no_bid)
                net_sell = no_bid - fee
                pnl_pct  = (no_bid - entry_price) / entry_price * 100 if entry_price else float("nan")
                decision = "★SELL" if net_sell > no_prob else "hold"
                table.append(
                    f"  {symbol:<40}  entry={entry_price:.3f}"
                    f"  no_bid={no_bid:.3f}  no_model={no_prob:.3f}"
                    f"  net_sell={net_sell:.3f}  pnl={pnl_pct:+.1f}%  → {decision} [NO]"
                )
                if net_sell > no_prob:
                    rows_to_sell.append((pos, "model_overpriced"))
            else:
                yes_bid = float(price_row.get("yes_bid", 0) or 0)
                if yes_bid <= 0 or yes_bid != yes_bid:
                    ask_s = f"{yes_ask:.3f}" if yes_ask == yes_ask else "  n/a"
                    table.append(
                        f"  {symbol:<40}  entry={entry_price:.3f}  ask={ask_s}"
                        f"  bid=  n/a  model={model_prob:.3f}  → no bid, holding"
                    )
                    continue
                fee      = taker_fee(yes_bid)
                net_sell = yes_bid - fee
                pnl_pct  = (yes_bid - entry_price) / entry_price * 100 if entry_price else float("nan")
                ask_s    = f"{yes_ask:.3f}" if yes_ask == yes_ask else "  n/a"
                decision = "★SELL" if net_sell > model_prob else "hold"
                table.append(
                    f"  {symbol:<40}  entry={entry_price:.3f}  ask={ask_s}"
                    f"  bid={yes_bid:.3f}  model={model_prob:.3f}"
                    f"  net_sell={net_sell:.3f}  pnl={pnl_pct:+.1f}%  → {decision}"
                )
                if net_sell > model_prob:
                    rows_to_sell.append((pos, "model_overpriced"))

        logger.info("Open positions (%d):\n%s", len(positions), "\n".join(table))

        if self._output_dir:
            self._write_positions_csv(positions, price_map)

        if not rows_to_sell:
            return pd.DataFrame()
        df = pd.DataFrame([r for r, _ in rows_to_sell])
        df["reason"] = [reason for _, reason in rows_to_sell]
        return df

    def _write_positions_csv(self, positions: pd.DataFrame,
                             price_map: dict) -> None:
        ts = datetime.now(tz=timezone.utc).isoformat()
        fieldnames = ["ts", "ticker", "side", "entry_price", "entry_bid", "entry_spread_pct",
                      "yes_ask", "yes_bid", "no_ask", "no_bid",
                      "model_prob", "net_sell", "pnl_pct", "decision"]
        rows = []
        for _, pos in positions.iterrows():
            symbol      = pos.get("symbol")
            entry_price = float(pos.get("avg_entry_price", 0) or 0)
            model_prob  = self._recommender.get_model_prob(symbol) if symbol else None
            price_row   = price_map.get(symbol) if symbol else None
            kalshi_side = self._recommender.get_position_side(symbol) if symbol else "yes"

            def _f(col):
                if price_row is None:
                    return ""
                v = price_row.get(col, "") or ""
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return ""

            yes_ask = _f("yes_ask")
            yes_bid = _f("yes_bid")
            no_ask  = _f("no_ask")
            no_bid  = _f("no_bid")

            # Track first bid seen as entry bid (use the relevant side's bid)
            side_bid = no_bid if kalshi_side == "no" else yes_bid
            if symbol and side_bid and symbol not in self._entry_bids:
                self._entry_bids[symbol] = side_bid
            entry_bid = self._entry_bids.get(symbol, "")
            entry_spread_pct = (
                round((entry_bid - entry_price) / entry_price * 100, 2)
                if entry_bid != "" and entry_price else ""
            )

            if kalshi_side == "no":
                if model_prob is not None and no_bid:
                    fee      = taker_fee(no_bid)
                    net_sell = no_bid - fee
                    no_prob  = 1.0 - model_prob
                    pnl_pct  = round((no_bid - entry_price) / entry_price * 100, 2) if entry_price else ""
                    decision = "sell" if net_sell > no_prob else "hold"
                else:
                    net_sell = decision = pnl_pct = ""
            else:
                if model_prob is not None and yes_bid:
                    fee      = taker_fee(yes_bid)
                    net_sell = yes_bid - fee
                    pnl_pct  = round((yes_bid - entry_price) / entry_price * 100, 2) if entry_price else ""
                    decision = "sell" if net_sell > model_prob else "hold"
                else:
                    net_sell = decision = pnl_pct = ""

            def _r(v):
                return round(v, 4) if isinstance(v, float) else v

            rows.append({
                "ts":               ts,
                "ticker":           symbol or "",
                "side":             kalshi_side,
                "entry_price":      round(entry_price, 4),
                "entry_bid":        _r(entry_bid),
                "entry_spread_pct": entry_spread_pct,
                "yes_ask":          _r(yes_ask),
                "yes_bid":          _r(yes_bid),
                "no_ask":           _r(no_ask),
                "no_bid":           _r(no_bid),
                "model_prob":       round(model_prob, 4) if model_prob is not None else "",
                "net_sell":         _r(net_sell) if net_sell != "" else "",
                "pnl_pct":          pnl_pct,
                "decision":         decision,
            })
        path = self._output_dir / "positions_eval.csv"
        write_header = not path.exists()
        with path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)


class CompositeSellEngine:
    """
    Combines multiple sell engines with OR logic: sell if any engine says so.

    Parameters
    ----------
    engines:
        Two or more objects satisfying SellDecisionProtocol.
    """

    def __init__(self, *engines: SellDecisionProtocol) -> None:
        self._engines = engines

    @property
    def post_evaluate(self) -> Callable[[pd.DataFrame, list], None] | None:
        return None

    def evaluate(
        self,
        positions: pd.DataFrame,
        signals:   list,
        prices:    pd.DataFrame,
    ) -> pd.DataFrame:
        if positions.empty:
            return pd.DataFrame()

        # Collect symbols and their reasons from each engine
        symbol_reasons: dict[str, str] = {}
        for engine in self._engines:
            result = engine.evaluate(positions, signals, prices)
            if result.empty or "symbol" not in result.columns:
                continue
            for _, row in result.iterrows():
                sym = row["symbol"]
                if sym not in symbol_reasons:
                    symbol_reasons[sym] = row.get("reason", "")

        if not symbol_reasons:
            return pd.DataFrame()

        df = positions[positions["symbol"].isin(symbol_reasons)].copy()
        df["reason"] = df["symbol"].map(symbol_reasons)
        return df
