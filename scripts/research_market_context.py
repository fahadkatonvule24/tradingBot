"""Is the market panel's context worth anything as a forecast?

    python scripts/research_market_context.py            # needs data/ from `tradebot fetch` (H1)

The dashboard's market panel shows two figures that traders commonly read as forecasts. This
script measures both on the six default instruments with the dashboard's own code
(``tradebot.market``). Declared before running, and not changed afterwards:

1. Currency strength. Every 24 H1 bars (non-overlapping samples), score each currency over the
   last 24 bars exactly as the panel does. For each instrument, the strength of its base minus
   the strength of its quote says which way the last day favoured. Does the next 24 bars' move
   go the same way? Reported: share of samples where it did, and the average next-day move in
   that direction, in ATR, with a t-statistic.

2. Day range used. At 13:00 UTC (London afternoon, New York opening), how much of the 20-day
   average daily range has the day already covered? Bucketed into under 50%, 50-100% and 100%
   or more. Reported per bucket: how much further the day's range extended by its close, as a
   share of the average range. The common belief is that a day that has used its average range
   has little left.

A trading day starts at the broker's daily open (17:00 New York); 13:00 UTC is hour 16 of it.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import market
from tradebot.data import load_bars, load_spec
from tradebot.indicators import atr

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "XAUUSD")
DAY_BARS = 24
CHECK_HOUR_UTC = 13


def t_stat(values: np.ndarray) -> float:
    return (
        float(values.mean() / (values.std(ddof=1) / math.sqrt(len(values)))) if len(values) > 2 else math.nan
    )


def load(data: Path) -> tuple[dict[str, pd.DataFrame], dict]:
    frames, specs = {}, {}
    for symbol in SYMBOLS:
        bars = load_bars(data / f"{symbol}_H1.csv")
        frames[symbol] = bars.assign(atr=atr(bars, 14))
        specs[symbol] = load_spec(data, symbol)
    return frames, specs


def strength_test(frames: dict[str, pd.DataFrame], specs: dict) -> None:
    common = sorted(set.intersection(*(set(f["time"]) for f in frames.values())))
    aligned = {s: f[f["time"].isin(common)].reset_index(drop=True) for s, f in frames.items()}
    n = len(common)
    rows = []
    for i in range(200, n - DAY_BARS, DAY_BARS):
        window = {s: f.iloc[: i + 1] for s, f in aligned.items()}
        scores = market.strength(window, specs, DAY_BARS)
        for symbol, frame in aligned.items():
            legs = market.legs(specs[symbol])
            if len(legs) != 2:
                continue
            lean = scores[legs[0][0]] - scores[legs[1][0]]
            atr_now = float(frame["atr"].iloc[i])
            if lean == 0 or not atr_now > 0:
                continue
            ahead = (float(frame["close"].iloc[i + DAY_BARS]) - float(frame["close"].iloc[i])) / atr_now
            own = (float(frame["close"].iloc[i]) - float(frame["close"].iloc[i - DAY_BARS])) / atr_now
            rows.append({"symbol": symbol, "lean": np.sign(lean), "strong": abs(lean) >= 1.0,
                         "ahead": ahead, "own": np.sign(own)})  # fmt: skip
    df = pd.DataFrame(rows)
    df["follow"] = df["lean"] * df["ahead"]
    print(f"1. Currency strength, {common[0]:%Y-%m-%d} to {common[-1]:%Y-%m-%d}, {len(df):,} samples")
    print("   next 24 H1 bars in the direction the last day's strength favoured")
    for label, part in (("all samples", df), ("strong lean (|diff| >= 1)", df[df["strong"]])):
        print(f"   {label:<28} {len(part):>6,}  went that way {(part['follow'] > 0).mean():6.1%}  "
              f"average {part['follow'].mean():+.3f} ATR  t = {t_stat(part['follow'].to_numpy()):+.2f}")  # fmt: skip
    for symbol, part in df.groupby("symbol"):
        print(
            f"     {symbol:<8} {len(part):>5,}  {(part['follow'] > 0).mean():6.1%}  {part['follow'].mean():+.3f} ATR"
        )
    agree = (df["lean"] == df["own"]).mean()
    print(f"   strength pointed the same way as the instrument's own last day on {agree:.0%} of samples")


def range_test(frames: dict[str, pd.DataFrame]) -> None:
    print(f"\n2. Day range used by {CHECK_HOUR_UTC}:00 UTC, and how much further the day went")
    out = []
    for symbol, bars in frames.items():
        b = bars.assign(day=(bars["time"] + market.WEEK_SHIFT).dt.normalize())
        days = b.groupby("day").agg(high=("high", "max"), low=("low", "min"), n=("time", "size"))
        days = days[days["n"] >= 20]
        adr = (days["high"] - days["low"]).rolling(market.ADR_DAYS).mean().shift(1)
        upto = (
            b[b["time"].dt.hour < CHECK_HOUR_UTC].groupby("day").agg(high=("high", "max"), low=("low", "min"))
        )
        joined = days.join(upto.add_suffix("_sofar")).join(adr.rename("adr")).dropna()
        sofar = joined["high_sofar"] - joined["low_sofar"]
        used = sofar / joined["adr"]
        further = ((joined["high"] - joined["low"]) - sofar) / joined["adr"]
        out.append(pd.DataFrame({"symbol": symbol, "used": used, "further": further}))
    df = pd.concat(out)
    buckets = pd.cut(
        df["used"], [0, 0.5, 1.0, np.inf], right=False, labels=["under 50%", "50-100%", "100% or more"]
    )
    for label, part in df.groupby(buckets, observed=True):
        print(f"   {label:<14} {len(part):>6,} days  extended a further {part['further'].mean():5.0%} of the average "
              f"range (median {part['further'].median():4.0%}); went on by 25% or more on {(part['further'] >= 0.25).mean():4.0%}")  # fmt: skip


def main() -> None:
    data = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
    frames, specs = load(data)
    strength_test(frames, specs)
    range_test(frames)


if __name__ == "__main__":
    main()
