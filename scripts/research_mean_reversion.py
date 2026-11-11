"""Mean reversion, the Bollinger + RSI template, measured once (README: "Trading principles").

    python scripts/research_mean_reversion.py H1,M15      # needs data/ from `tradebot fetch`

Declared before running, one run each, nothing tuned:
  template   regime ADX(14) < 25; a bar closes outside the Bollinger band (20, 2) with RSI(14) < 30
             (buy) or > 70 (sell); enter when the next bar closes back inside the band; stop 1 ATR
             beyond the extreme of those two bars; target the middle band as it stood at entry.
  rr15       the same, taking only trades whose reward:risk at entry is at least 1.5
  noregime   the template without the ADX regime filter (does the filter matter?)

Simple fill model, same conventions as the bot's backtester: bid bars, buys pay the spread, sells
exit at the ask, max(recorded spread, configured floor), $7 per lot round trip, a gap fills at the
open, a bar touching both stop and target counts as a stop, entries only 07-21 UTC on weekdays,
one position per symbol.
"""

import sys
from dataclasses import replace

import numpy as np
import pandas as pd

from tradebot.config import load_config
from tradebot.data import load_dataset
from tradebot.indicators import adx, atr, rsi

base = load_config("config/config.json")


def trades_for(symbol, bars, spec, bt, tf_minutes, variant):
    o, h, low, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    close = bars["close"]
    mid = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    upper, lower = (mid + 2 * sd).to_numpy(float), (mid - 2 * sd).to_numpy(float)
    mid = mid.to_numpy(float)
    r = rsi(close, 14).to_numpy(float)
    a = atr(bars, 14).to_numpy(float)
    x = adx(bars, 14).to_numpy(float)
    spread = np.maximum(bars["spread"].to_numpy(float), bt.min_spread_points) * spec.point
    commission = bt.commission_per_lot / (spec.tick_value / spec.tick_size)  # as a price distance
    closes_at = bars["time"] + pd.Timedelta(minutes=tf_minutes)
    hour, weekday = closes_at.dt.hour.to_numpy(), closes_at.dt.day_of_week.to_numpy()
    out, busy_until = [], -1
    for i in range(60, len(c) - 1):
        if i <= busy_until or not np.isfinite(a[i]) or not np.isfinite(x[i]):
            continue
        if not (7 <= hour[i] < 21) or weekday[i] >= 5:
            continue
        if variant != "noregime" and x[i] >= 25:
            continue
        side = 0
        if c[i - 1] < lower[i - 1] and r[i - 1] < 30 and c[i] > lower[i]:
            side = 1
        elif c[i - 1] > upper[i - 1] and r[i - 1] > 70 and c[i] < upper[i]:
            side = -1
        if not side:
            continue
        if side > 0:
            entry, stop, target = c[i] + spread[i], min(low[i - 1], low[i]) - a[i], mid[i]
        else:
            entry, stop, target = c[i], max(h[i - 1], h[i]) + a[i], mid[i]
        risk, reward = (entry - stop) * side, (target - entry) * side
        if risk <= 0 or reward <= 0:
            continue
        if variant == "rr15" and reward / risk < 1.5:
            continue
        exit_price, j = None, i
        for j in range(i + 1, min(i + 2000, len(c))):
            off = 0.0 if side > 0 else spread[j]  # sells are bought back at the ask
            oj, hj, lj = o[j] + off, h[j] + off, low[j] + off
            if side > 0:
                if oj <= stop or oj >= target:
                    exit_price = oj
                elif lj <= stop:
                    exit_price = stop
                elif hj >= target:
                    exit_price = target
            else:
                if oj >= stop or oj <= target:
                    exit_price = oj
                elif hj >= stop:
                    exit_price = stop
                elif lj <= target:
                    exit_price = target
            if exit_price is not None:
                break
        if exit_price is None:
            exit_price = c[j] + (0.0 if side > 0 else spread[j])
        busy_until = j
        out.append(
            (
                closes_at.iloc[i],
                symbol,
                side,
                ((exit_price - entry) * side - commission) / risk,
                reward / risk,
                j - i,
            )
        )
    return out


for tf in sys.argv[1].split(",") if len(sys.argv) > 1 else ["H1", "M15"]:
    cfg = replace(base, timeframe=tf)
    data, specs = load_dataset("data", list(cfg.symbols), tf)
    for variant in ("template", "rr15", "noregime"):
        rows = []
        for symbol, bars in data.items():
            rows += trades_for(
                symbol, bars, specs[symbol], cfg.for_symbol(symbol).backtest, cfg.bar_minutes, variant
            )
        t = pd.DataFrame(rows, columns=["time", "symbol", "side", "r", "rr", "bars"])
        weeks = (t["time"].max() - t["time"].min()) / pd.Timedelta(weeks=1)
        tstat = t["r"].mean() / (t["r"].std(ddof=1) / len(t) ** 0.5)
        per = "  ".join(f"{s} {g['r'].mean():+.2f}" for s, g in t.groupby("symbol"))
        print(
            f"{tf:4} {variant:9} {t['time'].min():%Y-%m} to {t['time'].max():%Y-%m}  trades {len(t):5}  "
            f"{len(t) / weeks:4.1f}/wk  win {(t['r'] > 0).mean():5.1%}  avg reward:risk {t['rr'].mean():.2f}  "
            f"avgR {t['r'].mean():+.3f}  totalR {t['r'].sum():+7.1f}  t {tstat:+.2f}  held {t['bars'].mean():.0f} bars",
            flush=True,
        )
        print(f"       per symbol: {per}", flush=True)
