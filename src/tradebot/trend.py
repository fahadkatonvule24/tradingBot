"""The trend on the trading timeframe: is there one, how old, how strong, and is it running out?

A trend here is what the EMA setup already trades: the fast EMA above the slow one is an uptrend,
below it a downtrend, and it ends when they cross. That end is late by construction, so this
module also watches for the things that tend to come first. Each is read on closed bars only:

  price       a bar closed on the wrong side of the slow EMA
  divergence  price made a new ``breakout_period``-bar closing extreme with weaker momentum (MACD
              histogram) than at the previous one, at least 5 bars earlier in the same trend
  momentum    the MACD histogram is on the other side of zero
  converging  the gap between the averages is at most half its widest of this trend, and still
              narrowing
  fading      ADX is at least 20% below its highest of this trend (which reached 25 or more)

Nobody can know that a trend is about to stop, so what these signs have been worth was measured
(``scripts/research_trend_signs.py``, reported in the README). The thresholds above were fixed
before that measurement and not changed after it. What did change is how the signs are read. The
first version counted them equally, and the count turned out to mean nothing: with two signs
standing a trend ended within 10 bars no more often than on any other bar, and divergence alone
came before an ending slightly less often than chance. Two signs carried the information, so the
state follows them:

  ending     ``price`` stands: about three trends in four ended within 10 bars
  weakening  ``converging`` stands without ``price``
  intact     neither, whatever else is showing

The other three are still listed, as context, and the page says they are not reliable alone.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from tradebot.broker.base import SymbolSpec
from tradebot.config import StrategyParams
from tradebot.strategy import trend_columns

SIGNS = ("price", "converging", "divergence", "momentum", "fading")
LEADING = ("price", "converging")
"""The two signs that came before a trend's end more often than chance. They set the state."""
STATES = ("none", "intact", "weakening", "ending")
ADX_PEAK_MIN = 25.0
ADX_FADE = 0.8
GAP_PEAK_MIN = 0.5
"""ATR. A trend whose averages never got this far apart has nothing to converge from."""
GAP_SHRINK = 0.5
DIVERGENCE_APART = 5
DIVERGENCE_LIFE = 10
"""Bars a divergence stands, unless a new extreme with stronger momentum clears it first."""


def trend_frame(features: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """One row per closed bar: ``direction`` (+1 up, -1 down, 0 none), ``age`` in bars since the
    averages crossed, the five signs as booleans and ``level``, how many of them stand.

    Causal: row i uses bars 0..i only. ``adx_peak``, ``gap`` and ``gap_peak`` are kept for the text.
    """
    n = len(features)
    close, atr_, fast, slow, adx_, hist = (
        features[k].to_numpy(dtype=float)
        for k in ("close", "atr", "ema_fast", "ema_slow", "adx", "macd_hist")
    )
    spread = fast - slow
    direction = np.where(np.isfinite(spread), np.sign(spread), 0).astype(int)
    prior_high = features["close"].rolling(p.breakout_period).max().shift(1).to_numpy(dtype=float)
    prior_low = features["close"].rolling(p.breakout_period).min().shift(1).to_numpy(dtype=float)

    age = np.zeros(n, dtype=int)
    adx_peak, gap, gap_peak = (np.full(n, np.nan) for _ in range(3))
    flags = {key: np.zeros(n, dtype=bool) for key in SIGNS}
    previous, start, peak, widest, last_gap = 0, 0, 0.0, 0.0, np.nan
    extremes: list[tuple[int, float]] = []  # (bar, momentum in the trend's direction) of each new extreme
    diverged_until = -1
    for i in range(n):
        d = int(direction[i])
        if d == 0:
            previous = 0
            continue
        if d != previous:  # the averages crossed: a new trend
            start, peak, widest, last_gap, extremes, diverged_until = i, 0.0, 0.0, np.nan, [], -1
        previous = d
        age[i] = i - start + 1
        if np.isfinite(adx_[i]):
            peak = max(peak, adx_[i])
        width = spread[i] * d / atr_[i] if np.isfinite(atr_[i]) and atr_[i] > 0 else np.nan
        if np.isfinite(width):
            widest = max(widest, width)
        adx_peak[i], gap[i], gap_peak[i] = peak, width, widest

        flags["price"][i] = (close[i] - slow[i]) * d < 0
        flags["momentum"][i] = hist[i] * d < 0
        flags["fading"][i] = peak >= ADX_PEAK_MIN and adx_[i] <= ADX_FADE * peak
        flags["converging"][i] = (
            widest >= GAP_PEAK_MIN
            and width <= GAP_SHRINK * widest
            and np.isfinite(last_gap)
            and width < last_gap
        )
        last_gap = width

        edge = prior_high[i] if d > 0 else prior_low[i]
        if np.isfinite(edge) and (close[i] - edge) * d > 0 and np.isfinite(hist[i]):
            momentum = hist[i] * d
            earlier = [m for at, m in extremes if i - at >= DIVERGENCE_APART]
            diverged_until = i + DIVERGENCE_LIFE if earlier and momentum < earlier[-1] else -1
            extremes.append((i, momentum))
        flags["divergence"][i] = i <= diverged_until

    out = pd.DataFrame({"direction": direction, "age": age, "adx_peak": adx_peak, "gap": gap,
                        "gap_peak": gap_peak, **flags}, index=features.index)  # fmt: skip
    out["level"] = out[list(SIGNS)].sum(axis=1).astype(int)
    out.loc[out["direction"] == 0, "level"] = 0
    out["state"] = np.where(
        out["direction"] == 0, "none",
        np.where(out["price"], "ending", np.where(out["converging"], "weakening", "intact")),
    )  # fmt: skip
    return out


def strength(adx_value: float, p: StrategyParams) -> str:
    if not np.isfinite(adx_value):
        return "unknown"
    if adx_value < p.adx_min:
        return "weak"
    return "strong" if adx_value >= 40 else "steady"


def _sign_text(key: str, row: pd.Series, bar: pd.Series, p: StrategyParams, spec: SymbolSpec) -> str:
    up = row["direction"] > 0
    if key == "price":
        return (
            f"Price closed {'below' if up else 'above'} EMA {p.slow_ema} ({bar['ema_slow']:.{spec.digits}f})."
        )
    if key == "divergence":
        return (
            f"Divergence: price made a new {p.breakout_period}-bar closing {'high' if up else 'low'} "
            f"with weaker momentum than at the previous one."
        )
    if key == "momentum":
        return f"Momentum has turned: the MACD histogram is {'negative' if up else 'positive'}."
    if key == "converging":
        return (
            f"The averages are closing in: EMA {p.fast_ema} is {row['gap']:.1f} ATR from EMA {p.slow_ema}, "
            f"down from {row['gap_peak']:.1f} at its widest."
        )
    return (
        f"Strength is fading: ADX {bar['adx']:.0f}, down from a high of {row['adx_peak']:.0f} in this trend."
    )


def describe(
    features: pd.DataFrame, p: StrategyParams, timeframe: str, spec: SymbolSpec, bar_minutes: int
) -> dict[str, Any]:
    """The trend as of the last closed bar, in the shape the dashboard and the alerts use."""
    frame = trend_frame(features, p)
    row, bar = frame.iloc[-1], features.iloc[-1]
    direction, age = int(row["direction"]), int(row["age"])
    step = pd.Timedelta(minutes=bar_minutes)
    higher = [
        {"timeframe": column[6:], "direction": int(bar[column])} for column in trend_columns(features)
    ] or [{"timeframe": p.trend_timeframe, "direction": int(bar["trend"])}]
    out: dict[str, Any] = {
        "direction": direction,
        "timeframe": timeframe,
        "age": age,
        "since": None,
        "adx": float(bar["adx"]) if pd.notna(bar["adx"]) else None,
        "strength": strength(float(bar["adx"]), p) if direction else "",
        "signs": [],
        "level": 0,
        "state": "none",
        "headline": f"No {timeframe} trend yet: the averages are still warming up",
        "higher": higher,
        "previous": None,
    }
    if not direction:
        return out
    word = "uptrend" if direction > 0 else "downtrend"
    started = len(frame) - age  # the bar on which the averages crossed
    out["since"] = (features["time"].iloc[started] + step).to_pydatetime()
    if started > 0 and int(frame["direction"].iloc[started - 1]) == -direction:
        was = frame.iloc[started - 1]
        out["previous"] = {"direction": -direction, "age": int(was["age"])}
    signs = [key for key in SIGNS if bool(row[key])]
    out["signs"] = [
        {"key": key, "text": _sign_text(key, row, bar, p, spec), "leading": key in LEADING} for key in signs
    ]
    out["level"] = len(signs)
    out["state"] = state = str(row["state"])
    bars = f"{age} bar{'' if age == 1 else 's'}"
    if age == 1:
        crossed = "above" if direction > 0 else "below"
        out["headline"] = f"New {timeframe} {word}: EMA {p.fast_ema} crossed {crossed} EMA {p.slow_ema}"
    elif state == "ending":
        out["headline"] = f"{timeframe} {word} is likely ending ({bars} old)"
    elif state == "weakening":
        out["headline"] = f"{timeframe} {word} is weakening ({bars} old)"
    else:
        out["headline"] = f"{timeframe} {word}, {bars} old, {out['strength']}"
    return out


class TrendAlerts:
    """Says a trend's start once, and each step up in its state (weakening, ending) once per trend.

    A state that eases and returns is not repeated: one trend gives at most three messages.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[Any, int, int]] = {}  # symbol -> (since, direction, highest state said)

    def new(self, symbol: str, trend: dict[str, Any]) -> str:
        """ "" (nothing to say), "start", "weakening" or "ending"."""
        if not trend["direction"]:
            return ""
        key = (trend["since"], trend["direction"])
        rank = STATES.index(trend["state"])
        seen = self._seen.get(symbol)
        if seen is None or seen[:2] != key:
            first_look = seen is None
            self._seen[symbol] = (*key, rank)
            if trend["age"] == 1:
                return "start"
            if first_look:  # the dashboard has just started: say only what matters right now
                return trend["state"] if rank >= STATES.index("weakening") else ""
            return "start"
        if rank > seen[2]:
            self._seen[symbol] = (*key, rank)
            return trend["state"]
        return ""

    def clear(self) -> None:
        self._seen.clear()
