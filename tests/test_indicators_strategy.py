from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from tradebot.config import StrategyParams
from tradebot.indicators import adx, atr, ema, rsi
from tradebot.strategy import compute_signals, higher_timeframe_trend


def textbook_adx(df: pd.DataFrame, p: int) -> np.ndarray:
    """Wilder's original ADX with SMA seeding, written as a plain loop."""
    h, low, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    tr, pdm, ndm = [], [], []
    for i in range(1, len(df)):
        up, down = h[i] - h[i - 1], low[i - 1] - low[i]
        pdm.append(up if up > down and up > 0 else 0.0)
        ndm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1])))

    def smooth(v):
        out = [sum(v[:p])]
        for x in v[p:]:
            out.append(out[-1] - out[-1] / p + x)
        return out

    dx = []
    for t, a, b in zip(smooth(tr), smooth(pdm), smooth(ndm)):
        pdi, ndi = 100 * a / t, 100 * b / t
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)
    out = [sum(dx[:p]) / p]
    for x in dx[p:]:
        out.append((out[-1] * (p - 1) + x) / p)
    return np.array(out)


def test_adx_matches_textbook_and_is_bounded(bars):
    ours = adx(bars, 14).dropna().to_numpy()
    ref = textbook_adx(bars, 14)
    # Different seeding converges quickly; compare the tails.
    assert np.allclose(ours[-1000:], ref[-1000:], atol=0.5)
    assert ours.min() >= 0 and ours.max() <= 100


def test_adx_regression_v23_inflated_by_period(bars):
    """v23 smoothed DX with a running sum: values ~14x too large, so `adx >= 20` always passed."""
    values = adx(bars, 14).dropna()
    assert (values < 20).mean() > 0.1  # a real ADX spends time below 20 on this data


def test_rsi_and_ema_basics():
    rising = pd.Series(np.arange(1.0, 60.0))
    assert rsi(rising, 14).dropna().eq(100).all()
    falling = pd.Series(np.arange(60.0, 1.0, -1))
    assert rsi(falling, 14).dropna().lt(1e-9).all()
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    expected = [np.nan, np.nan, 2.25, 3.125, 4.0625]  # alpha = 0.5, seeded with the first value
    assert np.allclose(ema(s, 3).to_numpy(), expected, equal_nan=True)


def test_atr_is_positive(bars):
    assert (atr(bars, 14).dropna() > 0).all()


@pytest.mark.parametrize("name", ["ema_cross", "breakout", "pullback", "retest"])
def test_signals_are_causal(bars, name):
    """No look-ahead: results on any prefix equal the full run's rows for that prefix."""
    p = StrategyParams(name=name)
    full = compute_signals(bars, p, "M15")
    for cut in (1500, 2777, 4000):
        prefix = compute_signals(bars.iloc[:cut], p, "M15")
        cols = ["signal", "raw_signal", "trend", "atr", "adx"]
        pd.testing.assert_frame_equal(prefix[cols], full[cols].iloc[:cut], check_dtype=False)


def test_higher_timeframe_trend_ignores_unfinished_htf_bar(bars):
    p = StrategyParams(trend_timeframe="H4", trend_ema=20)
    base = bars.iloc[:2000]
    trend = higher_timeframe_trend(base, "M15", p)
    # Wildly change the last 3 base bars (inside an H4 bar still forming at their close):
    altered = base.copy()
    altered.loc[altered.index[-3:], ["open", "high", "low", "close"]] = 9.99
    altered_trend = higher_timeframe_trend(altered, "M15", p)
    assert (trend.iloc[:-3] == altered_trend.iloc[:-3]).all()


@pytest.mark.parametrize("name", ["ema_cross", "breakout", "pullback", "retest"])
def test_each_strategy_signals_both_ways(bars, name):
    signals = compute_signals(bars, StrategyParams(name=name, use_trend_filter=False), "M15")
    assert (signals["signal"] == 1).sum() > 0 and (signals["signal"] == -1).sum() > 0
    assert set(signals["signal"].unique()) <= {-1, 0, 1}


@pytest.mark.parametrize("name", ["ema_cross", "breakout", "pullback", "retest"])
def test_trend_filter_only_allows_trades_with_the_trend(bars, name):
    signals = compute_signals(bars, StrategyParams(name=name), "M15")
    taken = signals[signals["signal"] != 0]
    assert len(taken) > 0
    assert (taken["signal"] == taken["trend"]).all()


def test_filters_record_why_a_signal_was_blocked(bars):
    p = StrategyParams(use_adx=False, use_macd=False, use_sr_filter=False, rsi_buy_max=100, rsi_sell_min=0)
    df = compute_signals(bars, p, "M15")
    blocked = df[(df["raw_signal"] != 0) & (df["signal"] == 0)]
    assert set(blocked["block"]) <= {"trend", "warmup"}
    unfiltered = compute_signals(bars, replace(p, use_trend_filter=False), "M15")
    warm = unfiltered["atr"].notna() & unfiltered["ema_slow"].notna()
    assert (unfiltered.loc[warm, "signal"] == unfiltered.loc[warm, "raw_signal"]).all()
