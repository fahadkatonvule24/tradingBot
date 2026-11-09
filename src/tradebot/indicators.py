"""Technical indicators, vectorised with pandas.

All smoothing follows the textbook definitions (Wilder's RMA for RSI/ATR/ADX, EMA with
alpha = 2/(n+1)). Every function is causal: the value at row i depends only on rows <= i.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average (a.k.a. SMMA), alpha = 1/period."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = rma(delta.clip(lower=0), period)
    loss = rma(-delta.clip(upper=0), period)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def true_range(bars: pd.DataFrame) -> pd.Series:
    prev_close = bars["close"].shift(1)
    ranges = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev_close).abs(), (bars["low"] - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1, skipna=False).fillna(bars["high"] - bars["low"])


def atr(bars: pd.DataFrame, period: int) -> pd.Series:
    return rma(true_range(bars), period)


def adx(bars: pd.DataFrame, period: int) -> pd.Series:
    """Average Directional Index, bounded 0-100.

    v23 smoothed DX with a running *sum*, which inflated ADX by a factor of ``period``
    (~14x) and made the trend filter a no-op. The final smoothing here is an average.
    """
    up = bars["high"].diff()
    down = -bars["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=bars.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=bars.index)
    tr = rma(true_range(bars), period)
    plus_di = 100 * rma(plus_dm, period) / tr
    minus_di = 100 * rma(minus_dm, period) / tr
    total = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / total).fillna(0.0)
    return rma(dx, period).where(tr.notna())


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, signal_line, line - signal_line
