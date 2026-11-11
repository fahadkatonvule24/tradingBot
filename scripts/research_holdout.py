"""One-shot holdout test of the research candidate (ema_cross | H1 | run exits) vs the current config.

Swap is not in the simulator, so it is charged afterwards from today's broker rates:
per lot per night, triple on Wednesday's rollover (22:00 UTC winter / 21:00 summer ~ 17:00 New York).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from research_families import DATA, HOLDOUT_START, OUT, family_config

from tradebot.backtest import run_backtest
from tradebot.config import load_config
from tradebot.data import load_bars, load_spec
from tradebot.optimize import apply_params

SWAP = {  # USD per lot per night (long, short), read from the terminal on 2026-11-10
    "EURUSD": (-0.70, -1.00), "GBPUSD": (-0.20, -2.20), "USDJPY": (-0.06, -0.38),
    "AUDUSD": (6.30, -14.80), "USDCAD": (-0.56, -0.28), "XAUUSD": (-12.60, -4.60),
}  # fmt: skip


def swap_cost(trade) -> float:
    def ny(t):
        return pd.Timestamp(t).tz_localize("UTC").tz_convert("America/New_York").tz_localize(None)

    first = (ny(trade.entry_time) - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(days=1)
    last = (ny(trade.exit_time) - pd.Timedelta(hours=17)).normalize()
    nights = 0
    day = first
    while day <= last:
        if day.day_of_week < 5:
            nights += 3 if day.day_of_week == 2 else 1
        day += pd.Timedelta(days=1)
    rate = SWAP[trade.symbol][0 if trade.side > 0 else 1]
    return rate * trade.volume * nights


def report(name: str, result) -> None:
    trades = result.trades
    r = np.array([t.r_multiple for t in trades])
    swaps = np.array([swap_cost(t) for t in trades])
    risk = np.array([t.risk_amount for t in trades])
    r_net = r + swaps / risk
    m = result.metrics

    def t(x):
        return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))

    wins, losses = r_net[r_net > 0], r_net[r_net <= 0]
    print(f"\n{name}")
    print(
        f"  {result.start:%Y-%m-%d} -> {result.end:%Y-%m-%d} · {m.trades} trades · return {m.return_pct:+.2f}% "
        f"· max DD {m.max_drawdown_pct:.1f}% · PF {m.profit_factor:.2f}"
    )
    print(
        f"  before swap: avg {r.mean():+.3f}R, t = {t(r):+.2f} | swap total ${swaps.sum():+.0f} "
        f"({swaps.sum() / result.config.backtest.initial_balance * 100:+.2f}% of balance)"
    )
    print(
        f"  after swap : avg {r_net.mean():+.3f}R, t = {t(r_net):+.2f}, winners {len(wins) / len(r_net):.1%}, "
        f"avg win {wins.mean():+.2f}R, avg loss {losses.mean():+.2f}R"
    )
    print(
        result.per_symbol[["trades", "win_rate", "net_profit", "profit_factor", "avg_r"]].round(2).to_string()
    )


def main() -> None:
    base = load_config("config/config.json")
    # the rules these runs used: a single H4 trend filter, and any close through a level is a break
    base = base.with_strategy(trend_timeframes=("H4",), breakout_min_atr=0.0)
    data = {s: load_bars(DATA / f"{s}_H1.csv") for s in base.symbols}
    specs = {s: load_spec(DATA, s) for s in base.symbols}
    htf = {(s, "H4"): load_bars(DATA / f"{s}_H4.csv") for s in base.symbols}
    research = json.loads(OUT.read_text())
    params = research["ema_cross|H1|run"]["final_params"]
    cfg, _ = family_config(base, "ema_cross", "H1", "run")
    candidate = apply_params(cfg, params)
    print("candidate parameters (best on the last research window):", params)
    report("CANDIDATE on the holdout", run_backtest(candidate, data, specs, start=HOLDOUT_START, htf=htf))
    report("CURRENT CONFIG on the holdout", run_backtest(base, data, specs, start=HOLDOUT_START, htf=htf))


if __name__ == "__main__":
    main()
