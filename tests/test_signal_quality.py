"""Causal signal-quality checks shared by entries, advice and backtests."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from test_engine import flat, setup, step
from test_reversal import row as reversal_row

from tradebot.backtest import run_backtest
from tradebot.config import STRATEGIES, BotConfig, ConfigError, StrategyParams, config_from_dict
from tradebot.data import preset_spec
from tradebot.reversal import EXIT, assess, watch_position
from tradebot.signal_quality import add_quality_features, quality_checks
from tradebot.strategy import compute_signals


def quality_params(**changes):
    return replace(StrategyParams(use_signal_quality_filter=True, quality_period=3), **changes)


def candle_frame(closes=None):
    close = pd.Series(closes if closes is not None else [100, 101, 102, 103, 104, 105], dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.8,
            "high": close + 0.2,
            "low": close - 1.0,
            "close": close,
            "atr": 1.2,
            "ema_fast": close - 0.5,
        }
    )


def checks_for(frame, p=None, side=1):
    return dict(quality_checks(frame, p or quality_params(), side))


def ready_metrics():
    """A clean buy at the configured inclusive boundaries."""
    return pd.DataFrame(
        {
            "quality_data": [True],
            "quality_efficiency": [0.2],
            "quality_body": [0.2],
            "quality_close": [0.65],
            "quality_range_atr": [2.5],
            "quality_extension_atr": [2.0],
        }
    )


def test_quality_metrics_measure_candle_shape_and_prior_volatility():
    df = candle_frame()
    add_quality_features(df, quality_params())
    row = df.iloc[-1]
    assert row["quality_efficiency"] == pytest.approx(1.0)
    assert row["quality_body"] == pytest.approx(0.8 / 1.2)
    assert row["quality_close"] == pytest.approx(1 / 1.2)
    assert row["quality_range_atr"] == pytest.approx(1.0)
    assert row["quality_extension_atr"] == pytest.approx(0.5 / 1.2)
    assert row["quality_data"]
    assert all(passes.iloc[-1] for passes in checks_for(df).values())


def test_quality_checks_are_symmetric_for_buys_and_sells():
    up = candle_frame()
    down = up.copy()
    down["open"], down["close"] = 210 - up["open"], 210 - up["close"]
    down["high"], down["low"] = 210 - up["low"], 210 - up["high"]
    down["ema_fast"] = 210 - up["ema_fast"]
    for df in (up, down):
        add_quality_features(df, quality_params())
    for key, values in checks_for(up, side=1).items():
        pd.testing.assert_series_equal(values, checks_for(down, side=-1)[key], check_names=False)
    assert not checks_for(up, side=-1)["quality_candle"].iloc[-1]
    assert not checks_for(down, side=1)["quality_candle"].iloc[-1]
    joined = pd.concat([up.tail(1), down.tail(1)], ignore_index=True)
    assert all(passes.all() for passes in checks_for(joined, side=pd.Series([1, -1])).values())


def test_gap_spike_uses_previous_atr_even_when_current_atr_expands():
    df = candle_frame()
    df.loc[5, ["open", "high", "low", "close", "atr", "ema_fast"]] = [109.2, 110.2, 109, 110, 99, 110]
    add_quality_features(df, quality_params())
    # The previous close is 104: the gap makes the true range 6.2, despite a 1.2 candle range.
    assert df.loc[5, "quality_range_atr"] == pytest.approx(6.2 / 1.2)
    assert not checks_for(df)["quality_spike"].iloc[-1]


def test_efficiency_uses_only_the_window_before_the_trigger_bar():
    p = quality_params(quality_period=4)
    df = candle_frame([100, 101, 100, 101, 100, 101, 120])
    add_quality_features(df, p)
    assert df["quality_efficiency"].iloc[-1] == 0
    assert not checks_for(df, p)["quality_chop"].iloc[-1]
    assert df["quality_efficiency"].iloc[:5].isna().all()
    flat_prices = candle_frame([100] * 8)
    add_quality_features(flat_prices, p)
    assert flat_prices["quality_efficiency"].iloc[-1] == 0
    assert not checks_for(flat_prices, p)["quality_chop"].iloc[-1]


@pytest.mark.parametrize("name", STRATEGIES)
def test_chop_filter_does_not_reject_a_break_from_consolidation(name):
    p = quality_params(name=name)
    df = ready_metrics()
    df["quality_efficiency"] = 0.0
    checks = checks_for(df, p)
    if name in ("ema_cross", "pullback"):
        assert not checks["quality_chop"].iloc[-1]
    else:
        assert "quality_chop" not in checks
        assert all(passes.all() for passes in checks.values())


@pytest.mark.parametrize(
    ("column", "value", "failed_check"),
    [
        ("quality_efficiency", 0.199, "quality_chop"),
        ("quality_body", 0.199, "quality_candle"),
        ("quality_close", 0.649, "quality_candle"),
        ("quality_range_atr", 2.501, "quality_spike"),
        ("quality_extension_atr", 2.001, "quality_extension"),
    ],
)
def test_thresholds_include_the_boundary_but_reject_the_wrong_side(column, value, failed_check):
    df = ready_metrics()
    assert all(passes.all() for passes in checks_for(df).values())
    df[column] = value
    assert not checks_for(df)[failed_check].iloc[0]


@pytest.mark.parametrize(
    ("column", "value"),
    [("open", np.nan), ("close", np.inf), ("high", 103.0), ("low", 107.0)],
)
def test_invalid_candle_data_cannot_pass_quality_checks(column, value):
    df = candle_frame()
    df.loc[5, column] = value
    add_quality_features(df, quality_params())
    assert not df["quality_data"].iloc[-1]
    assert not any(passes.iloc[-1] for passes in checks_for(df).values())


@pytest.mark.parametrize("baseline", [0.0, -1.0, np.nan, np.inf])
def test_invalid_previous_atr_fails_closed(baseline):
    df = candle_frame()
    df.loc[4, "atr"] = baseline
    add_quality_features(df, quality_params())
    assert not checks_for(df)["quality_data"].iloc[-1]


def test_zero_range_and_missing_metrics_fail_closed():
    df = candle_frame()
    df.loc[5, ["open", "high", "low", "close"]] = 105.0
    add_quality_features(df, quality_params())
    assert not checks_for(df)["quality_data"].iloc[-1]
    missing = checks_for(pd.DataFrame(index=[0]))
    assert not missing["quality_data"].iloc[0]
    assert not any(passes.iloc[0] for passes in missing.values())


@pytest.mark.parametrize("name", STRATEGIES)
def test_quality_filter_preserves_triggers_existing_reasons_and_exit_signals(bars, name):
    plain_p = StrategyParams(name=name)
    plain = compute_signals(bars, plain_p, "M15")
    strict = compute_signals(
        bars, replace(plain_p, use_signal_quality_filter=True, quality_max_range_atr=0.000001), "M15"
    )
    assert (plain["signal"] != 0).any()
    assert not strict["signal"].any()
    pd.testing.assert_series_equal(strict["raw_signal"], plain["raw_signal"])
    pd.testing.assert_series_equal(strict["exit_signal"], plain["signal"], check_names=False)
    existing = plain["block"] != ""
    pd.testing.assert_series_equal(strict.loc[existing, "block"], plain.loc[existing, "block"])
    new = plain["signal"] != 0
    assert strict.loc[new, "block"].str.startswith("quality_").all()


@pytest.mark.parametrize("name", STRATEGIES)
def test_disabled_filter_restores_original_entries_even_with_strict_thresholds(bars, name):
    p = StrategyParams(name=name)
    plain = compute_signals(bars, p, "M15")
    disabled = compute_signals(
        bars,
        replace(p, use_signal_quality_filter=False, quality_max_range_atr=0.000001, quality_min_efficiency=1),
        "M15",
    )
    pd.testing.assert_frame_equal(
        disabled[["raw_signal", "signal", "block"]], plain[["raw_signal", "signal", "block"]]
    )
    pd.testing.assert_series_equal(disabled["exit_signal"], disabled["signal"], check_names=False)


@pytest.mark.parametrize("name", STRATEGIES)
def test_quality_features_and_decisions_do_not_repaint_past_bars(bars, name):
    p = quality_params(name=name)
    full = compute_signals(bars.iloc[:650], p, "M15")
    columns = [c for c in full if c.startswith("quality_")] + ["raw_signal", "exit_signal", "signal", "block"]
    for length in (31, 125, 417):
        prefix = compute_signals(bars.iloc[:length], p, "M15")
        pd.testing.assert_frame_equal(prefix[columns], full[columns].iloc[:length])


def test_quality_filtered_entry_does_not_open_a_position(config, eurusd):
    config, sim, engine = setup(config.with_strategy(use_signal_quality_filter=True), eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=0, raw_signal=1, exit_signal=1, block="quality_spike"))
    assert not sim.positions(config.magic)
    assert not any(event.kind == "open" for event in engine.events)


def test_quality_rejection_does_not_suppress_an_existing_reverse_exit(config, eurusd):
    config, sim, engine = setup(config.with_strategy(use_signal_quality_filter=True), eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1, exit_signal=1))
    step(
        engine, sim, config, eurusd, flat(15, signal=0, raw_signal=-1, exit_signal=-1, block="quality_candle")
    )
    assert not sim.positions(config.magic)
    assert len(sim.trades) == 1
    assert sim.trades[0].exit_reason == "opposite signal"


def test_raw_reverse_trigger_rejected_by_old_filters_does_not_force_an_exit(config, eurusd):
    config, sim, engine = setup(config.with_strategy(use_signal_quality_filter=True), eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1, exit_signal=1))
    step(engine, sim, config, eurusd, flat(15, signal=0, raw_signal=-1, exit_signal=0, block="trend"))
    assert len(sim.positions(config.magic)) == 1
    assert not sim.trades


def test_backtest_preserves_quality_exits_and_falls_back_for_older_supplied_features(config, eurusd):
    config = replace(config.with_strategy(use_signal_quality_filter=True), symbols=("EURUSD", "GBPUSD"))
    modern = pd.DataFrame(
        [
            flat(0, signal=1, raw_signal=1, exit_signal=1),
            flat(15, signal=0, raw_signal=-1, exit_signal=-1, block="quality_candle"),
            flat(30, signal=0, raw_signal=0, exit_signal=0),
        ]
    )
    older = pd.DataFrame([flat(0, signal=1, raw_signal=1), flat(15, signal=-1, raw_signal=-1), flat(30)])
    frames = {"EURUSD": modern, "GBPUSD": older}
    result = run_backtest(
        config, frames, {"EURUSD": eurusd, "GBPUSD": preset_spec("GBPUSD")}, features=frames
    )
    # Concatenation introduces NaN exit_signal into the older frame. Its signal still closes
    # the buy; the quality-blocked modern sell closes the buy without opening a replacement.
    for symbol in frames:
        trades = [trade for trade in result.trades if trade.symbol == symbol]
        assert trades[0].exit_reason == "opposite signal"
        assert trades[0].exit_time == modern["time"].iloc[1] + pd.Timedelta(minutes=15)
        assert len(trades) == (1 if symbol == "EURUSD" else 2)


@pytest.mark.parametrize(
    ("signal", "exit_signal", "expected"),
    [(0, -1, EXIT), (0, 0, ""), (-1, np.nan, EXIT)],
)
def test_reversal_watch_uses_the_same_exit_decision_as_the_engine(signal, exit_signal, expected):
    frame = pd.DataFrame(
        [
            reversal_row(0, exit_signal=0),
            reversal_row(15, signal=signal, raw_signal=-1, exit_signal=exit_signal),
            reversal_row(30, exit_signal=0),
        ]
    )
    opened = (frame["time"].iloc[0] + pd.Timedelta(minutes=15)).to_pydatetime()
    watch = watch_position(frame, 1, opened, 15)
    assert watch.level == expected
    assert list(watch.signs) == (["signal"] if expected else [])
    if expected:
        found = assess(frame, 1, opened, quality_params(), "M15", preset_spec("EURUSD"))
        assert "strategy filters for an opposite-signal exit" in found.signs[0]["text"]
        assert "Entry-quality checks do not block this exit" in found.signs[0]["text"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("use_signal_quality_filter", "false"),
        ("use_signal_quality_filter", 1),
        ("quality_period", 1),
        ("quality_period", 2.5),
        ("quality_period", True),
        ("quality_period", "20"),
        ("quality_min_efficiency", -0.1),
        ("quality_min_efficiency", 1.1),
        ("quality_min_body_ratio", np.nan),
        ("quality_min_body_ratio", "0.2"),
        ("quality_min_close_position", 0.49),
        ("quality_min_close_position", 1.01),
        ("quality_max_range_atr", 0),
        ("quality_max_range_atr", np.inf),
        ("quality_max_extension_atr", -1),
        ("quality_max_extension_atr", None),
    ],
)
def test_quality_configuration_rejects_invalid_values_and_types(key, value):
    with pytest.raises(ConfigError, match=key):
        BotConfig(strategy=replace(StrategyParams(), **{key: value})).validate()


def test_quality_settings_validate_overrides_round_trip_and_history_requirements():
    with pytest.raises(ConfigError, match=r"EURUSD.*quality_period"):
        BotConfig(symbols=("EURUSD",), overrides={"EURUSD": {"strategy": {"quality_period": 1}}}).validate()
    p = quality_params(
        quality_period=300, quality_min_efficiency=0, quality_min_body_ratio=1, quality_min_close_position=0.5
    )
    cfg = BotConfig(symbols=("EURUSD",), strategy=p)
    cfg.validate()
    assert config_from_dict(cfg.to_dict(), env={}).strategy == p
    assert p.warmup_bars >= 302
