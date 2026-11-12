"""Do the "trend is running out" signs come before a trend actually stops?

    python scripts/research_trend_signs.py H1,M15,M30,M5        # needs data/ from `tradebot fetch`

The dashboard counts five signs against the trend on screen (``tradebot.trend``): price on the
wrong side of the slow EMA, divergence, momentum turned, the averages converging, ADX fading. This
script asks what followed them in the stored history. Declared before running, thresholds as
written in ``tradebot/trend.py`` and not changed afterwards:

  A trend is a run of bars with EMA 10 on one side of EMA 50; it ends on the bar they cross.
  For every bar of every trend, by the number of signs standing on that bar (0, 1, 2, 3+):
    - how often the trend ended within the next 10 bars
    - how far price moved in the trend's direction over the next 10 bars, in ATR
  and the same for each sign on its own. Then, per trend: how many reached two signs before they
  ended, how many bars of notice that gave, and how many of those alarms were early (the trend
  ran another 20 bars or more).

One caveat is built in: "converging" and "price" are close relatives of the cross that defines the
end, so they predict it partly by construction. The move in price over the next 10 bars does not
have that problem, which is why both are reported.

Added after the first run, and said so because it was: counting the signs equally did not work
(two signs standing meant nothing), so the dashboard's state now follows the two signs that did
carry information. "ending" = price is on the wrong side of the slow EMA; "weakening" =
converging without that. The second block of output measures those states the same way, per
instrument as well, so the rule can be seen to hold on each one and not only in the total.
"""

import sys
from dataclasses import replace

import numpy as np
import pandas as pd

from tradebot.config import load_config
from tradebot.data import load_dataset
from tradebot.strategy import compute_signals
from tradebot.trend import SIGNS, trend_frame

HORIZON, LATE = 10, 20
base = load_config("config/config.json")


def study(symbol: str, bars: pd.DataFrame, timeframe: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    p = base.for_symbol(symbol).strategy
    features = compute_signals(bars, replace(p, use_trend_filter=False), timeframe)
    frame = trend_frame(features, p)
    close, atr_ = features["close"].to_numpy(float), features["atr"].to_numpy(float)
    direction = frame["direction"].to_numpy()
    n = len(frame)
    to_end = np.full(n, np.nan)  # bars until the direction changes
    nxt = np.nan
    for i in range(n - 1, -1, -1):
        if i < n - 1 and direction[i + 1] != direction[i]:
            nxt = 1
        elif np.isfinite(nxt):
            nxt += 1
        to_end[i] = nxt
    forward = np.full(n, np.nan)
    forward[:-HORIZON] = (close[HORIZON:] - close[:-HORIZON]) * direction[:-HORIZON] / atr_[:-HORIZON]
    table = frame.assign(to_end=to_end, forward=forward, symbol=symbol)
    table = table[(table["direction"] != 0) & np.isfinite(table["forward"]) & np.isfinite(table["to_end"])]
    table = table[features.loc[table.index, "adx"].notna()]

    trends = []
    trend_id = (frame["direction"] != frame["direction"].shift()).cumsum()
    for _, run in frame[frame["direction"] != 0].groupby(trend_id):
        last = run.index[-1]
        if last == frame.index[-1] or features.loc[run.index[0], "adx"] != features.loc[run.index[0], "adx"]:
            continue  # still running at the end of the data, or inside the warm-up
        alarm = run.index[run["level"] >= 2]
        weak = run.index[run["state"].isin(["weakening", "ending"])]
        ending = run.index[run["state"] == "ending"]
        trends.append({
            "length": len(run),
            "notice": (last - alarm[0] + 1) if len(alarm) else np.nan,
            "weak_notice": (last - weak[0] + 1) if len(weak) else np.nan,
            "end_notice": (last - ending[0] + 1) if len(ending) else np.nan,
        })  # fmt: skip
    return table, pd.DataFrame(trends)


def line(label: str, rows: pd.DataFrame, total: int) -> str:
    if rows.empty:
        return f"  {label:22} no bars"
    ends = (rows["to_end"] <= HORIZON).mean()
    return (f"  {label:22} {len(rows) / total:6.1%} of trend bars   ended within {HORIZON} bars {ends:5.1%}   "
            f"next {HORIZON} bars {rows['forward'].mean():+.3f} ATR (median {rows['forward'].median():+.3f})")  # fmt: skip


for tf in sys.argv[1].split(",") if len(sys.argv) > 1 else ["H1", "M15"]:
    data, _ = load_dataset("data", list(base.symbols), tf)
    tables, runs = zip(*(study(symbol, bars, tf) for symbol, bars in data.items()))
    table, trends = pd.concat(tables), pd.concat(runs)
    total = len(table)
    print(f"{tf}: {len(trends)} finished trends on {len(data)} instruments, median length "
          f"{trends['length'].median():.0f} bars; {total} trend bars", flush=True)  # fmt: skip
    print(line("any bar of a trend", table, total))
    for level, label in ((0, "no sign"), (1, "1 sign"), (2, "2 signs")):
        print(line(label, table[table["level"] == level], total))
    print(line("3 or more signs", table[table["level"] >= 3], total))
    for key in SIGNS:
        print(line(f"sign: {key}", table[table[key]], total))
    warned = trends["notice"].notna()
    notice = trends.loc[warned, "notice"]
    print(f"  per trend: {warned.mean():.1%} reached two signs before ending; notice median {notice.median():.0f} bars "
          f"(quartiles {notice.quantile(0.25):.0f} to {notice.quantile(0.75):.0f}); "
          f"{(notice > LATE).mean():.1%} of those alarms came more than {LATE} bars early", flush=True)  # fmt: skip
    print("  -- by state (the rule the dashboard uses)")
    for state in ("intact", "weakening", "ending"):
        print(line(f"state: {state}", table[table["state"] == state], total))
    for state in ("weakening", "ending"):
        rates = [(g["to_end"] <= HORIZON).mean() for _, g in table[table["state"] == state].groupby("symbol")]
        print(
            f"  state: {state:10} ended within {HORIZON} bars, per instrument: {min(rates):.1%} to {max(rates):.1%}"
        )
    for label, column in (("weakening or ending", "weak_notice"), ("ending", "end_notice")):
        seen = trends[column].dropna()
        print(f"  per trend: {len(seen) / len(trends):.1%} showed '{label}' before they ended; first shown a median "
              f"{seen.median():.0f} bars before (quartiles {seen.quantile(0.25):.0f} to {seen.quantile(0.75):.0f}); "
              f"{(seen > LATE).mean():.1%} came more than {LATE} bars early", flush=True)  # fmt: skip
