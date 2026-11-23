"""Grid search and walk-forward optimisation.

Walk-forward is the honest way to tune a strategy: split history into consecutive windows,
pick the best parameters on window k (in-sample), then trade them on window k+1, which the
search never saw (out-of-sample). Only the stitched out-of-sample results say anything about
the future; the gap between in-sample and out-of-sample performance shows how much of the
"edge" was curve fitting.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import pandas as pd

from tradebot.backtest import BacktestResult, prepare_features, run_backtest
from tradebot.broker.base import SymbolSpec
from tradebot.config import BotConfig
from tradebot.metrics import Metrics, compute_metrics, per_symbol

DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "ema_cross": {
        "strategy.fast_ema": [8, 13, 21],
        "strategy.slow_ema": [34, 55, 89],
        "strategy.adx_min": [15.0, 22.0],
        "risk.tp_atr": [2.0, 3.0],
    },
    "breakout": {
        "strategy.breakout_period": [20, 40, 60],
        "strategy.adx_min": [15.0, 22.0],
        "risk.sl_atr": [1.5, 2.5],
        "risk.tp_atr": [3.0, 4.5],
    },
    "pullback": {
        "strategy.pullback_rsi_period": [2, 3],
        "strategy.pullback_buy_below": [10.0, 20.0],
        "strategy.adx_min": [15.0, 22.0],
        "risk.tp_atr": [1.5, 2.5, 3.5],
    },
    "retest": {
        "strategy.retest_period": [20, 40],
        "strategy.retest_window": [6, 12],
        "strategy.retest_tolerance_atr": [0.15, 0.3],
        "risk.tp_atr": [2.0, 3.0],
    },
}

OBJECTIVES = ("sharpe", "profit_factor", "return_pct", "calmar")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fold:
    train: tuple[datetime, datetime]
    test: tuple[datetime, datetime]
    params: dict[str, Any]
    in_sample: Metrics
    out_of_sample: Metrics


@dataclass
class WalkForwardResult:
    strategy: str
    objective: str
    folds: list[Fold]
    oos_equity: pd.Series
    oos_metrics: Metrics
    oos_trades: list
    oos_per_symbol: pd.DataFrame
    combos: int
    final_params: dict[str, Any]
    """Best parameters on the most recent window: what a live re-tune would deploy."""

    @property
    def efficiency(self) -> float:
        """Out-of-sample / in-sample annualised return. ~1 = robust, <=0 = curve fit."""
        is_cagr = sum(f.in_sample.cagr_pct for f in self.folds) / len(self.folds)
        oos_cagr = sum(f.out_of_sample.cagr_pct for f in self.folds) / len(self.folds)
        return oos_cagr / is_cagr if is_cagr > 0 else float("nan")


def apply_params(config: BotConfig, params: dict[str, Any]) -> BotConfig:
    sections: dict[str, dict[str, Any]] = {}
    for key, value in params.items():
        section, name = key.split(".", 1)
        sections.setdefault(section, {})[name] = value
    changes = {section: replace(getattr(config, section), **values) for section, values in sections.items()}
    # An explicit grid value must actually be tested, even when a symbol overrides the same field.
    overrides = {
        symbol: {
            section: {name: value for name, value in values.items() if name not in sections.get(section, {})}
            for section, values in parts.items()
        }
        for symbol, parts in config.overrides.items()
    }
    return replace(config, **changes, overrides=overrides)


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    combos = [dict(zip(keys, values, strict=True)) for values in itertools.product(*grid.values())]
    return [c for c in combos if c.get("strategy.fast_ema", 0) < c.get("strategy.slow_ema", math.inf)]


def score(metrics: Metrics, objective: str, min_trades: int) -> float:
    if metrics.trades < min_trades:
        return -math.inf
    value = getattr(metrics, objective)
    return -math.inf if math.isnan(value) else min(value, 1e6)


def split_windows(start: datetime, end: datetime, folds: int) -> list[tuple[datetime, datetime]]:
    edges = pd.date_range(start, end, periods=folds + 2)
    return [(edges[i].to_pydatetime(), edges[i + 1].to_pydatetime()) for i in range(folds + 1)]


# --- worker side (module-level so ProcessPoolExecutor can pickle it) ------------------------------

_WORKER: dict[str, Any] = {}


def _init_worker(config: BotConfig, data: dict, specs: dict, windows: list, htf: dict | None = None) -> None:
    _WORKER.update(config=config, data=data, specs=specs, windows=windows, htf=htf)


def _evaluate(combo: dict[str, Any]) -> list[Metrics]:
    """Metrics of one parameter combination on every window (features computed once)."""
    config = apply_params(_WORKER["config"], combo)
    features = prepare_features(config, _WORKER["data"], _WORKER.get("htf"))
    out = []
    for start, end in _WORKER["windows"]:
        try:
            out.append(
                run_backtest(
                    config, _WORKER["data"], _WORKER["specs"], start=start, end=end, features=features
                ).metrics
            )
        except ValueError:
            out.append(compute_metrics(pd.Series([1.0]), [], 1.0))
    return out


def _evaluate_all(config, data, specs, windows, combos, jobs, htf=None) -> list[list[Metrics]]:
    if jobs > 1:
        try:
            with ProcessPoolExecutor(
                max_workers=jobs, initializer=_init_worker, initargs=(config, data, specs, windows, htf)
            ) as pool:
                return list(pool.map(_evaluate, combos, chunksize=max(1, len(combos) // (jobs * 4))))
        except (BrokenProcessPool, OSError) as exc:  # e.g. Windows pipe-handle errors in detached consoles
            log.warning("worker pool failed (%s); continuing in a single process", exc)
    _init_worker(config, data, specs, windows, htf)
    return [_evaluate(c) for c in combos]


def walk_forward(
    config: BotConfig,
    data: dict[str, pd.DataFrame],
    specs: dict[str, SymbolSpec],
    *,
    grid: dict[str, list[Any]] | None = None,
    folds: int = 4,
    objective: str = "sharpe",
    min_trades: int = 20,
    jobs: int | None = None,
    htf: dict[tuple[str, str], pd.DataFrame] | None = None,
) -> WalkForwardResult:
    if type(folds) is not int or folds < 1:
        raise ValueError("folds must be a positive integer")
    if jobs is not None and (type(jobs) is not int or jobs < 1):
        raise ValueError("jobs must be a positive integer or omitted")
    if type(min_trades) is not int or min_trades < 0:
        raise ValueError("min_trades must be a nonnegative integer")
    if not data or any(df.empty for df in data.values()):
        raise ValueError("walk-forward requires nonempty history for every symbol")
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {', '.join(OBJECTIVES)}")
    setups = set(config.setups().values())  # per-symbol overrides can name another setup
    if grid is None:
        if len(setups) > 1:
            raise ValueError(
                f"the symbols trade different setups ({', '.join(sorted(setups))}): optimise one at a "
                "time, e.g. --symbols GBPUSD XAUUSD --strategy breakout"
            )
        grid = DEFAULT_GRIDS[next(iter(setups))]
    combos = expand_grid(grid)
    if not combos:
        raise ValueError("grid has no parameter combinations to evaluate")
    start = min(df["time"].iloc[0] for df in data.values()).to_pydatetime()
    end = max(df["time"].iloc[-1] for df in data.values()).to_pydatetime()
    windows = split_windows(start, end, folds)
    jobs = jobs or max(1, (os.cpu_count() or 2) - 1)

    table = _evaluate_all(config, data, specs, windows, combos, jobs, htf)  # [combo][window] -> Metrics

    def best_for(window: int) -> int:
        scores = [score(row[window], objective, min_trades) for row in table]
        best = max(range(len(combos)), key=scores.__getitem__)
        if scores[best] == -math.inf:
            raise ValueError(
                f"no qualifying parameter set in training window {window + 1}: "
                f"min_trades={min_trades} and a valid {objective} score are required"
            )
        return best

    fold_results: list[Fold] = []
    oos_runs: list[BacktestResult] = []
    for k in range(folds):
        best = best_for(k)
        tuned = apply_params(config, combos[best])
        test_start, test_end = windows[k + 1]
        run = run_backtest(tuned, data, specs, start=test_start, end=test_end, htf=htf)
        oos_runs.append(run)
        fold_results.append(Fold(windows[k], windows[k + 1], combos[best], table[best][k], run.metrics))

    final = best_for(folds)
    equity, trades = _stitch(oos_runs, config.backtest.initial_balance)
    return WalkForwardResult(
        strategy=" + ".join(sorted(setups)),
        objective=objective,
        folds=fold_results,
        oos_equity=equity,
        oos_metrics=compute_metrics(equity, trades, config.backtest.initial_balance),
        oos_trades=trades,
        oos_per_symbol=per_symbol(trades),
        combos=len(combos),
        final_params=combos[final],
    )


def _stitch(runs: list[BacktestResult], initial: float) -> tuple[pd.Series, list]:
    """Chain out-of-sample windows: each starts from the previous window's ending equity."""
    pieces, trades, level = [], [], initial
    for run in runs:
        scale = level / run.config.backtest.initial_balance
        pieces.append(run.equity * scale)
        level = float(pieces[-1].iloc[-1])
        trades += [replace(t, pnl=t.pnl * scale, risk_amount=t.risk_amount * scale) for t in run.trades]
    equity = pd.concat(pieces)
    return equity[~equity.index.duplicated(keep="last")], trades
