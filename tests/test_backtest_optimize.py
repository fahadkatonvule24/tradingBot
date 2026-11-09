import itertools
from dataclasses import replace

import pandas as pd
import pytest

from tradebot.backtest import run_backtest
from tradebot.data import preset_spec, synthetic_bars
from tradebot.metrics import compute_metrics, drawdown
from tradebot.optimize import apply_params, expand_grid, split_windows, walk_forward


@pytest.fixture(scope="module")
def market():
    data = {"EURUSD": synthetic_bars(n=8000, seed=3), "GBPUSD": synthetic_bars(n=8000, seed=4, price=1.27)}
    specs = {s: preset_spec(s) for s in data}
    return data, specs


@pytest.fixture
def cfg(config):
    return replace(config, symbols=("EURUSD", "GBPUSD"))


def test_backtest_accounting_identity(cfg, market):
    data, specs = market
    result = run_backtest(cfg, data, specs)
    assert result.metrics.trades > 10
    # every dollar of equity change is explained by closed trades (all positions closed at the end)
    assert result.equity.iloc[-1] == pytest.approx(
        cfg.backtest.initial_balance + sum(t.pnl for t in result.trades)
    )
    assert set(result.per_symbol.index) <= {"EURUSD", "GBPUSD"}


def test_backtest_is_deterministic(cfg, market):
    data, specs = market
    a, b = run_backtest(cfg, data, specs), run_backtest(cfg, data, specs)
    assert a.metrics == b.metrics


def test_backtest_respects_window(cfg, market):
    data, specs = market
    start, end = data["EURUSD"]["time"].iloc[3000], data["EURUSD"]["time"].iloc[6000]
    result = run_backtest(cfg, data, specs, start=start, end=end)
    assert all(start <= t.entry_time <= end + pd.Timedelta(minutes=15) for t in result.trades)


def test_higher_costs_never_help(cfg, market):
    data, specs = market
    cheap = run_backtest(cfg, data, specs).metrics
    costly = run_backtest(
        replace(cfg, backtest=replace(cfg.backtest, commission_per_lot=20, min_spread_points=30)), data, specs
    ).metrics
    assert costly.net_profit < cheap.net_profit


def test_metrics_on_known_curve():
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    equity = pd.Series([100.0, 110.0, 99.0, 121.0, 110.0], index=idx)
    assert drawdown(equity).min() == pytest.approx(-0.1)
    m = compute_metrics(equity, [], 100.0)
    assert m.max_drawdown_pct == pytest.approx(10.0)
    assert m.return_pct == pytest.approx(10.0)


def test_grid_and_params():
    combos = expand_grid({"strategy.fast_ema": [10, 60], "strategy.slow_ema": [50, 80]})
    assert {(c["strategy.fast_ema"], c["strategy.slow_ema"]) for c in combos} == {
        (10, 50),
        (10, 80),
        (60, 80),
    }


def test_apply_params(cfg):
    tuned = apply_params(cfg, {"strategy.fast_ema": 5, "risk.tp_atr": 4.0})
    assert tuned.strategy.fast_ema == 5 and tuned.risk.tp_atr == 4.0 and tuned.risk.sl_atr == cfg.risk.sl_atr


def test_split_windows_are_contiguous():
    windows = split_windows(pd.Timestamp("2025-01-01"), pd.Timestamp("2025-06-01"), 4)
    assert len(windows) == 5
    assert all(a[1] == b[0] for a, b in itertools.pairwise(windows))


def test_walk_forward_trades_only_out_of_sample(cfg, market):
    data, specs = market
    grid = {"strategy.fast_ema": [8, 13], "strategy.slow_ema": [34, 55]}
    wf = walk_forward(cfg, data, specs, grid=grid, folds=2, min_trades=1, jobs=1)
    assert len(wf.folds) == 2 and wf.combos == 4
    first_test_start = wf.folds[0].test[0]
    assert all(t.entry_time >= first_test_start for t in wf.oos_trades)
    for fold in wf.folds:
        assert fold.train[1] <= fold.test[0]
    assert wf.oos_equity.index.is_monotonic_increasing
