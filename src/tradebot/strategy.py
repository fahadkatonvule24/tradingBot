"""Signal generation, shared by live trading and the backtester.

``compute_signals`` takes *closed* bars and returns one row per bar with the indicators, a raw
entry trigger, and the final ``signal`` (+1 buy, -1 sell, 0 none) after filters. It is fully
vectorised and causal: row i only uses information available when bar i closed, which the test
suite checks by comparing results on every prefix of the data against the full run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradebot.broker.clock import NEW_YORK
from tradebot.config import TIMEFRAME_MINUTES, StrategyParams
from tradebot.indicators import adx, atr, ema, macd, rsi

# Which shared filters make sense for each entry trigger. MACD momentum would veto almost every
# pullback entry by construction, and an S/R "room to resistance" check contradicts a breakout.
FILTERS = {
    "ema_cross": ("trend", "adx", "macd", "rsi", "sr", "fib"),
    "breakout": ("trend", "adx", "macd"),
    "pullback": ("trend", "adx", "sr", "fib"),
}

BLOCK_LABELS = {
    "trend": "against higher-timeframe trend",
    "adx": "ADX below minimum (ranging market)",
    "macd": "MACD momentum disagrees",
    "rsi": "RSI stretched",
    "sr": "too close to support/resistance",
    "fib": "not near a Fibonacci retracement",
    "warmup": "indicators still warming up",
}


FX_DAY_START_NEW_YORK = pd.Timedelta(hours=17)
"""The FX trading day starts at 17:00 New York, which is 00:00 on most brokers' server clocks."""


def resample(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate UTC bars to a higher timeframe, labelled by open time (MT5 style).

    Hour-multiple bins are aligned the way a broker's are: from 17:00 New York, so H4 bars open at
    17, 21, 01, 05, 09 and 13 New York time in summer and winter alike (22, 02, 06... UTC in winter).
    Real higher-timeframe bars from ``tradebot fetch`` are preferred when available.
    """
    ohlc = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if minutes < 60 or (24 * 60) % minutes:
        agg = bars.set_index("time").resample(f"{minutes}min", label="left", closed="left").agg(ohlc)
        return agg.dropna().reset_index()
    local = bars["time"].dt.tz_localize("UTC").dt.tz_convert(NEW_YORK).dt.tz_localize(None)
    opened = (local - FX_DAY_START_NEW_YORK).dt.floor(f"{minutes}min") + FX_DAY_START_NEW_YORK
    utc_open = (
        opened.dt.tz_localize(
            NEW_YORK, ambiguous=np.ones(len(opened), dtype=bool), nonexistent="shift_forward"
        )
        .dt.tz_convert("UTC")
        .dt.tz_localize(None)
    )
    agg = bars.groupby(utc_open.rename("time"), sort=True).agg(ohlc)
    return agg.dropna().reset_index()


def higher_timeframe_trend(
    bars: pd.DataFrame, timeframe: str, p: StrategyParams, htf: pd.DataFrame | None = None
) -> pd.Series:
    """+1 / -1 / 0 per base bar: is the last *completed* higher-timeframe close above its EMA?"""
    base_min = TIMEFRAME_MINUTES[timeframe]
    htf_min = TIMEFRAME_MINUTES[p.trend_timeframe]
    if htf_min <= base_min:
        line = ema(bars["close"], p.trend_ema)
        return np.sign(bars["close"] - line).fillna(0).astype(int)

    if htf is None:
        htf = resample(bars, htf_min)
    htf = htf.sort_values("time")
    state = np.sign(htf["close"] - ema(htf["close"], p.trend_ema)).fillna(0)
    right = pd.DataFrame(
        {"close_time": htf["time"] + pd.Timedelta(minutes=htf_min), "trend": state.to_numpy()}
    )
    left = pd.DataFrame(
        {"close_time": bars["time"] + pd.Timedelta(minutes=base_min), "order": np.arange(len(bars))}
    )
    merged = pd.merge_asof(left.sort_values("close_time"), right, on="close_time", direction="backward")
    merged = merged.sort_values("order")
    return pd.Series(merged["trend"].fillna(0).astype(int).to_numpy(), index=bars.index)


def _trigger(df: pd.DataFrame, p: StrategyParams) -> pd.Series:
    close = df["close"]
    if p.name == "ema_cross":
        fast, slow = df["ema_fast"], df["ema_slow"]
        up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        down = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    elif p.name == "breakout":
        upper = df["high"].rolling(p.breakout_period).max().shift(1)
        lower = df["low"].rolling(p.breakout_period).min().shift(1)
        up = (close > upper) & (close.shift(1) <= upper.shift(1))
        down = (close < lower) & (close.shift(1) >= lower.shift(1))
    elif p.name == "pullback":
        fast_rsi = rsi(close, p.pullback_rsi_period)
        up = (fast_rsi.shift(1) < p.pullback_buy_below) & (fast_rsi >= p.pullback_buy_below)
        down = (fast_rsi.shift(1) > p.pullback_sell_above) & (fast_rsi <= p.pullback_sell_above)
    else:  # pragma: no cover - validated in config
        raise ValueError(f"unknown strategy {p.name!r}")
    return up.astype(int) - down.astype(int)


def compute_signals(
    bars: pd.DataFrame, p: StrategyParams, timeframe: str, htf: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Indicators + filtered signal for every closed bar."""
    df = bars.reset_index(drop=True).copy()
    close = df["close"]
    df["atr"] = atr(df, p.atr_period)
    df["ema_fast"] = ema(close, p.fast_ema)
    df["ema_slow"] = ema(close, p.slow_ema)
    df["rsi"] = rsi(close, p.rsi_period)
    df["adx"] = adx(df, p.adx_period)
    df["macd_hist"] = macd(close, p.macd_fast, p.macd_slow, p.macd_signal)[2]
    df["trend"] = higher_timeframe_trend(df, timeframe, p, htf)
    df["resistance"] = df["high"].rolling(p.sr_lookback).max().shift(1)
    df["support"] = df["low"].rolling(p.sr_lookback).min().shift(1)

    raw = _trigger(df, p)
    long, short = raw > 0, raw < 0
    atr_ = df["atr"]
    applicable = FILTERS[p.name]
    checks: list[tuple[str, pd.Series]] = []  # (name, passes) evaluated in this order

    warm = atr_.notna() & df["ema_slow"].notna() & df["rsi"].notna()
    if "adx" in applicable and p.use_adx:
        warm &= df["adx"].notna()
    checks.append(("warmup", warm))
    if "trend" in applicable and p.use_trend_filter:
        checks.append(("trend", (long & (df["trend"] > 0)) | (short & (df["trend"] < 0))))
    if "adx" in applicable and p.use_adx:
        checks.append(("adx", df["adx"] >= p.adx_min))
    if "macd" in applicable and p.use_macd:
        hist = df["macd_hist"]
        checks.append(("macd", (long & (hist > 0)) | (short & (hist < 0))))
    if "rsi" in applicable:
        checks.append(
            ("rsi", (long & (df["rsi"] <= p.rsi_buy_max)) | (short & (df["rsi"] >= p.rsi_sell_min)))
        )
    if "sr" in applicable and p.use_sr_filter:
        room = np.where(long, df["resistance"] - close, close - df["support"]) / atr_
        checks.append(("sr", pd.Series(room >= p.sr_min_atr, index=df.index)))
    if "fib" in applicable and p.use_fib_filter:
        checks.append(("fib", _near_fib(df, p, long)))

    passed = pd.Series(True, index=df.index)
    block = pd.Series("", index=df.index, dtype=object)
    for name, ok in checks:
        ok = ok.fillna(False).astype(bool)
        newly_blocked = (raw != 0) & passed & ~ok
        block = block.mask(newly_blocked, name)
        passed &= ok

    df["raw_signal"] = raw.astype("int8")
    df["signal"] = raw.where(passed, 0).astype("int8")
    df["block"] = block
    return df


def _near_fib(df: pd.DataFrame, p: StrategyParams, long: pd.Series) -> pd.Series:
    high = df["high"].rolling(p.fib_lookback).max()
    low = df["low"].rolling(p.fib_lookback).min()
    span = high - low
    close = df["close"]
    distances = []
    for ratio in (0.382, 0.5, 0.618):
        level = np.where(long, high - span * ratio, low + span * ratio)
        distances.append((close - level).abs())
    nearest = pd.concat(distances, axis=1).min(axis=1)
    return nearest <= p.fib_tolerance_atr * df["atr"]


def explain(row: pd.Series, p: StrategyParams) -> str:
    """Human-readable reason for a signal row, for logs and the trade journal."""
    side = "buy" if row["raw_signal"] > 0 else "sell"
    trigger = {
        "ema_cross": f"EMA {p.fast_ema}/{p.slow_ema} cross {'up' if side == 'buy' else 'down'}",
        "breakout": f"{p.breakout_period}-bar {'high' if side == 'buy' else 'low'} breakout",
        "pullback": f"RSI({p.pullback_rsi_period}) pullback {'recovered' if side == 'buy' else 'rolled over'}",
    }[p.name]
    parts = [trigger, f"RSI {row['rsi']:.1f}", f"ADX {row['adx']:.1f}"]
    parts.append(
        f"{p.trend_timeframe} trend {'up' if row['trend'] > 0 else 'down' if row['trend'] < 0 else 'flat'}"
    )
    if row.get("block"):
        parts.append("blocked: " + BLOCK_LABELS.get(row["block"], row["block"]))
    return " · ".join(parts)
