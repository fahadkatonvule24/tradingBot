"""Causal entry-quality checks shared by the strategy and the advisor.

These rules flag observable risk conditions, not the probability of a losing trade. Baselines
come from bars before the trigger so a spike cannot raise its own volatility allowance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradebot.config import StrategyParams

QUALITY_LABELS = {
    "quality_data": "signal quality cannot be assessed from incomplete or invalid bars",
    "quality_chop": "choppy price action before the trigger",
    "quality_candle": "weak or rejecting signal candle",
    "quality_spike": "signal candle or gap too large versus prior volatility",
    "quality_extension": "entry stretched too far from the fast average",
}


def add_quality_features(df: pd.DataFrame, p: StrategyParams) -> None:
    """Add direction-neutral measurements to an indicator frame of completed bars."""
    prices = df[["open", "high", "low", "close"]].astype(float)
    open_, high, low, close = (prices[c] for c in prices)
    valid = (
        np.isfinite(prices).all(axis=1)
        & (high >= prices[["open", "close"]].max(axis=1))
        & (low <= prices[["open", "close"]].min(axis=1))
        & (high >= low)
    )
    # Invalid historical candles must not disappear inside a rolling average or EMA.
    history_ok = valid.shift(1, fill_value=False).rolling(p.quality_period + 1).sum().eq(p.quality_period + 1)
    prior_atr = df["atr"].shift(1)
    baseline = prior_atr.where(np.isfinite(prior_atr) & (prior_atr > 0))
    span = (high - low).where(high > low)
    prior_close = close.shift(1)
    true_range = pd.concat([high - low, (high - prior_close).abs(), (low - prior_close).abs()], axis=1).max(
        axis=1
    )
    path = close.diff().abs().rolling(p.quality_period).sum().shift(1)
    net = close.diff(p.quality_period).abs().shift(1)
    df["quality_efficiency"] = (net / path.where(path > 0)).mask(path.eq(0), 0.0).clip(0, 1)
    df["quality_body"] = (close - open_) / span
    df["quality_close"] = (close - low) / span
    df["quality_range_atr"] = true_range / baseline
    df["quality_extension_atr"] = (close - df["ema_fast"]) / baseline
    df["quality_data"] = (
        valid
        & history_ok
        & baseline.notna()
        & np.isfinite(df["ema_fast"])
        & np.isfinite(prior_close)
        & span.notna()
    )


def quality_checks(
    features: pd.DataFrame, p: StrategyParams, side: int | pd.Series
) -> list[tuple[str, pd.Series]]:
    """Named pass/fail series, for one watch side or each row's raw trigger direction.

    Missing measurements fail closed. The chop check applies to crosses and pullbacks only:
    consolidation before a breakout or retest is an expected part of those setups.
    """
    if not p.use_signal_quality_filter:
        return []

    def metric(name: str) -> pd.Series:
        values = pd.to_numeric(features.get(name, pd.Series(np.nan, index=features.index)), errors="coerce")
        return values.where(np.isfinite(values))

    direction = pd.Series(side, index=features.index)
    body, close = metric("quality_body"), metric("quality_close")
    range_atr, extension = metric("quality_range_atr"), metric("quality_extension_atr")
    efficiency = metric("quality_efficiency")
    valid = features.get("quality_data", pd.Series(False, index=features.index)).eq(True)
    valid &= direction.isin([-1, 1]) & body.notna() & close.between(0, 1)
    valid &= range_atr.gt(0) & extension.notna()
    checks = [("quality_data", valid)]
    if p.name in ("ema_cross", "pullback"):
        valid &= efficiency.between(0, 1)
        checks[0] = ("quality_data", valid)
        checks.append(("quality_chop", efficiency >= p.quality_min_efficiency))
    directional_close = close.where(direction > 0, 1 - close)
    checks.extend(
        [
            (
                "quality_candle",
                (body * direction >= p.quality_min_body_ratio)
                & (directional_close >= p.quality_min_close_position),
            ),
            ("quality_spike", range_atr <= p.quality_max_range_atr),
            ("quality_extension", extension * direction <= p.quality_max_extension_atr),
        ]
    )
    return [(name, (passed & valid).fillna(False).astype(bool)) for name, passed in checks]
