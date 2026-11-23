"""The bot joining a running trend after its signal (strategy.auto_join)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from conftest import at, feed
from test_engine import flat, setup, step

from tradebot import smc
from tradebot.strategy import join_signals


def joining(config):
    return replace(config, strategy=replace(config.strategy, auto_join=True))


def test_a_missed_signal_is_joined_within_the_window(config, eurusd):
    config, sim, engine = setup(joining(config), eurusd)
    feed(sim, "EURUSD", flat(0, signal=1, raw_signal=1))  # the bot was not running for this bar
    step(engine, sim, config, eurusd, flat(15, join_signal=1, join_from=pd.Timestamp(at(0))))
    (position,) = sim.positions(config.magic)
    assert position.side == 1 and "joining the buy signal of 02 Mar 10:00 UTC" in engine.events[-1].detail


def test_a_signals_only_symbol_is_never_entered_live_but_is_still_backtested(config, eurusd):
    """risk.auto_trade false: the trading loop neither takes the signal nor joins it later; a
    backtest (not live) measures the rules as for any other symbol."""
    quiet = replace(joining(config), overrides={"EURUSD": {"risk": {"auto_trade": False}}})
    config, sim, engine = setup(quiet, eurusd)
    engine.live = True
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1, atr=0.0010))
    assert not sim.positions(config.magic)
    signal = engine.events[-1]  # the plan, for the phone: never an order
    assert (signal.kind, signal.side) == ("signal", 1)
    assert signal.detail.startswith("BUY signal · signals only: the bot does not trade EURUSD")
    assert " · SL " in signal.detail and " · TP " in signal.detail and "0.16 lots" in signal.detail
    step(engine, sim, config, eurusd, flat(15, join_signal=1, join_from=pd.Timestamp(at(0))))
    assert not sim.positions(config.magic) and engine.events[-1] is signal  # no join either
    config, sim, backtest = setup(quiet, eurusd)
    step(backtest, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    assert len(sim.positions(config.magic)) == 1


def test_a_signals_only_signal_the_guards_would_skip_is_a_skip_not_an_alert(config, eurusd):
    quiet = replace(config, overrides={"EURUSD": {"risk": {"auto_trade": False}}})
    config, sim, engine = setup(quiet, eurusd, max_total_positions=0)  # every position slot is taken
    engine.live = True
    step(engine, sim, config, eurusd, flat(0, signal=-1, raw_signal=-1))
    event = engine.events[-1]
    assert event.kind == "skip" and event.detail.startswith("sell signal skipped: positions")
    assert "signals only" in event.detail and not sim.positions(config.magic)


def test_a_signal_is_joined_once_and_never_after_its_own_trade(config, eurusd):
    config, sim, engine = setup(joining(config), eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))  # traded on time
    (position,) = sim.positions(config.magic)
    sim.close(position, None, config.magic, "stopped")
    step(engine, sim, config, eurusd, flat(15, join_signal=1, join_from=pd.Timestamp(at(0))))
    assert not sim.positions(config.magic)  # the signal had its trade: no second go after the loss

    config, sim, engine = setup(joining(config), eurusd)
    feed(sim, "EURUSD", flat(0, signal=1, raw_signal=1))
    step(engine, sim, config, eurusd, flat(15, join_signal=1, join_from=pd.Timestamp(at(0))))
    (joined,) = sim.positions(config.magic)
    sim.close(joined, None, config.magic, "stopped")
    step(engine, sim, config, eurusd, flat(30, join_signal=1, join_from=pd.Timestamp(at(0))))
    assert not sim.positions(config.magic)  # joined once


def test_no_join_while_the_switch_is_off_or_already_in_the_market(config, eurusd):
    config, sim, engine = setup(config, eurusd)  # auto_join off
    step(engine, sim, config, eurusd, flat(15, join_signal=1, join_from=pd.Timestamp(at(0))))
    assert not sim.positions(config.magic)
    config, sim, engine = setup(joining(config), eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=-1, raw_signal=-1, trend=-1))  # a sell is open
    step(engine, sim, config, eurusd, flat(15, join_signal=-1, join_from=pd.Timestamp(at(0)), trend=-1))
    assert len(sim.positions(config.magic)) == 1


def frame(n: int = 12, signal_at: int = 3, side: int = 1) -> pd.DataFrame:
    df = pd.DataFrame({
        "time": pd.date_range("2026-03-02", periods=n, freq="h"), "open": 1.1, "high": 1.1003, "low": 1.0997,
        "close": 1.1, "atr": 0.001, "trend": side, "ema_fast": 1.1 + side * 0.001, "ema_slow": 1.1,
        "signal": 0, "raw_signal": 0,
    })  # fmt: skip
    df.loc[signal_at, ["signal", "raw_signal"]] = side
    return df


@pytest.fixture
def no_cautions(monkeypatch):
    monkeypatch.setattr(smc, "against", lambda found, i, side: [])


def test_the_join_window_is_the_five_bars_after_a_taken_signal(config, no_cautions):
    p = replace(config.strategy, auto_join=True, use_trend_filter=True, name="ema_cross")
    out = join_signals(frame(), p)
    assert list(np.flatnonzero(out["join_signal"].to_numpy())) == [4, 5, 6, 7, 8]
    assert (out["join_from"].iloc[4:9] == pd.Timestamp("2026-03-02 03:00")).all()
    turned = frame()
    turned.loc[6:, "ema_fast"] = 1.099  # the averages crossed back: the trend is gone
    assert list(np.flatnonzero(join_signals(turned, p)["join_signal"].to_numpy())) == [4, 5]
    assert not join_signals(frame(), replace(p, use_trend_filter=False))["join_signal"].any()


def test_no_join_at_an_opposite_zone_or_after_a_sweep_against(config, monkeypatch):
    p = replace(config.strategy, auto_join=True, use_trend_filter=True, name="ema_cross")
    monkeypatch.setattr(
        smc, "against", lambda found, i, side: ["zone"] if i == 5 else ["sweep"] if i == 7 else ["gap"]
    )
    joins = list(np.flatnonzero(join_signals(frame(), p)["join_signal"].to_numpy()))
    assert joins == [4, 6, 8]  # a gap made no consistent difference to joins: it does not stop one


def test_a_newer_trigger_ends_the_window_as_on_the_dashboard(config, no_cautions):
    """The dashboard's JOIN refers to the newest trigger: a filtered one after the signal shows
    "filtered out", not JOIN, so the bot must not join there either."""
    p = replace(config.strategy, auto_join=True, use_trend_filter=True, name="ema_cross")
    filtered = frame()
    filtered.loc[5, "raw_signal"] = 1  # a buy trigger the filters blocked, two bars after the signal
    assert list(np.flatnonzero(join_signals(filtered, p)["join_signal"].to_numpy())) == [4]


def test_no_join_after_the_break_it_rode_failed(config, no_cautions):
    p = replace(config.strategy, auto_join=True, use_trend_filter=True, name="breakout")
    failed = frame().assign(failed_side=0, failed_level=np.nan)
    failed.loc[5, ["failed_side", "failed_level"]] = [1, 1.1005]  # closed back below the broken level
    joins = list(np.flatnonzero(join_signals(failed, p)["join_signal"].to_numpy()))
    assert joins == [4]  # the dashboard says "break failed" from bar 5 on, and so does the bot
    recovered = failed.copy()
    recovered.loc[7:, "close"] = 1.1006  # back above the level: the failure did not stick
    assert list(np.flatnonzero(join_signals(recovered, p)["join_signal"].to_numpy())) == [4, 7, 8]


def test_the_backtest_decides_joins_while_flat(config, eurusd):
    """A join is only possible with no position on the symbol, and the backtest used to skip
    every flat bar without a signal, so a backtest could never show a join."""
    from tradebot.backtest import run_backtest

    signal_at = pd.Timestamp(at(0))
    rows = [flat(15 * k) for k in range(12)]
    for k in (1, 2):  # the join window: no signal on these bars, only the join
        rows[k] = flat(15 * k, join_signal=1, join_from=signal_at)
    bars = pd.DataFrame(rows).assign(exit_signal=0)
    bars["join_signal"] = bars["join_signal"].fillna(0).astype(int)
    result = run_backtest(joining(config), {"EURUSD": bars}, {"EURUSD": eurusd}, features={"EURUSD": bars})
    assert result.trades and result.trades[0].entry_time == pd.Timestamp(at(30))  # bar 1 closed at :30


def test_the_join_window_is_validated(config):
    from tradebot.config import ConfigError

    for bad in (0, 13):
        with pytest.raises(ConfigError, match="join_window_bars"):
            replace(config, strategy=replace(config.strategy, join_window_bars=bad)).validate()
