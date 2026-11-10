"""Strategy research with a locked holdout (results: README, "Trying to improve the strategy").

    python scripts/research_families.py     # walk-forward every family on pre-holdout data
    python scripts/research_holdout.py      # then test the one candidate, once, on the holdout

Families (fixed before looking at any result): {ema_cross, breakout} x {H1, H4, D1} x
{target exits (current), run exits (no fixed target, trailing stop only)}.
Research data ends at HOLDOUT_START; the holdout is only touched by holdout.py.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.config import load_config
from tradebot.data import load_bars, load_spec
from tradebot.optimize import DEFAULT_GRIDS, walk_forward
from tradebot.strategy import resample

HOLDOUT_START = pd.Timestamp("2024-11-01")
OUT = Path("reports") / "research_results.json"
DATA = Path("data")


def bars_for(symbol: str, tf: str) -> pd.DataFrame:
    if tf == "D1":
        d1 = resample(load_bars(DATA / f"{symbol}_H4.csv"), 1440)
        d1["spread"] = 0
        return d1
    return load_bars(DATA / f"{symbol}_{tf}.csv")


def family_config(base, strategy: str, tf: str, exits: str):
    cfg = replace(base, timeframe=tf).with_strategy(name=strategy)
    if tf == "H4":
        cfg = cfg.with_strategy(trend_timeframe="D1")
        cfg = replace(cfg, schedule=replace(cfg.schedule, session="all"))
    if tf == "D1":
        cfg = cfg.with_strategy(trend_timeframe="D1")
        cfg = replace(cfg, schedule=replace(cfg.schedule, session="all"))
        # Daily bars close at the 17:00 New York rollover, when spreads are widest: triple the floor.
        cfg = replace(
            cfg,
            backtest=replace(cfg.backtest, min_spread_points=30.0),
            overrides={
                "XAUUSD": {"backtest": {"min_spread_points": 75.0}},
                "USDJPY": {"backtest": {"min_spread_points": 36.0}},
            },
        )
    grid = dict(DEFAULT_GRIDS[strategy])
    if exits == "run":
        cfg = replace(
            cfg,
            risk=replace(cfg.risk, tp_atr=30.0),
            management=replace(cfg.management, partial_tp=False, breakeven_r=1000.0, trail_start_r=0.5),
        )
        grid = {k: v for k, v in grid.items() if k not in ("risk.tp_atr", "risk.sl_atr")}
        grid["risk.sl_atr"] = [2.0, 3.0]
        grid["management.trail_atr"] = [3.0, 4.5]
    return cfg, grid


def summarise(wf) -> dict:
    r = np.array([t.r_multiple for t in wf.oos_trades])
    m = wf.oos_metrics
    t_stat = float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))) if len(r) > 2 and r.std() > 0 else 0.0
    per = wf.oos_per_symbol
    return {
        "trades": int(m.trades), "return_pct": m.return_pct, "max_dd": m.max_drawdown_pct, "sharpe": m.sharpe,
        "pf": m.profit_factor, "avg_r": m.avg_r, "win_rate": m.win_rate, "t_stat": t_stat,
        "fold_returns": [f.out_of_sample.return_pct for f in wf.folds],
        "fold_params": [f.params for f in wf.folds],
        "final_params": wf.final_params,
        "per_symbol": {s: {"trades": int(per.loc[s, "trades"]), "avg_r": float(per.loc[s, "avg_r"]),
                           "net": float(per.loc[s, "net_profit"])} for s in per.index},
        "years": m.years,
    }  # fmt: skip


def main() -> None:
    base = load_config("config/config.json")
    only = sys.argv[1:]
    OUT.parent.mkdir(exist_ok=True)
    results = json.loads(OUT.read_text()) if OUT.exists() else {}
    for tf in ("D1", "H4", "H1"):
        data = {s: bars_for(s, tf) for s in base.symbols}
        data = {s: d[d["time"] < HOLDOUT_START].reset_index(drop=True) for s, d in data.items()}
        specs = {s: load_spec(DATA, s) for s in base.symbols}
        htf = None
        if tf == "H1":
            htf = {(s, "H4"): load_bars(DATA / f"{s}_H4.csv") for s in base.symbols}
        for strategy in ("ema_cross", "breakout"):
            for exits in ("target", "run"):
                key = f"{strategy}|{tf}|{exits}"
                if key in results or (only and key not in only):
                    continue
                cfg, grid = family_config(base, strategy, tf, exits)
                started = time.time()
                wf = walk_forward(
                    cfg, data, specs, grid=grid, folds=4, min_trades=10 if tf == "D1" else 20, htf=htf
                )
                results[key] = summarise(wf)
                OUT.write_text(json.dumps(results, indent=1))
                s = results[key]
                print(
                    f"{key:24} trades {s['trades']:5} ret {s['return_pct']:+7.1f}% dd {s['max_dd']:5.1f}% "
                    f"pf {s['pf']:.2f} avgR {s['avg_r']:+.3f} t {s['t_stat']:+.2f} "
                    f"folds {[round(x, 1) for x in s['fold_returns']]} ({time.time() - started:.0f}s)",
                    flush=True,
                )


if __name__ == "__main__":
    main()
