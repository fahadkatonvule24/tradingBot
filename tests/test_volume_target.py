"""Breakout extras: volume confirmation, the measured-move target, and a minimum reward:risk."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from conftest import FakeMT5, make_sim
from test_advisor_ui import SYMBOL, features
from test_engine import flat, step
from test_retest import BREAK_UP, frame

from tradebot.advisor import filter_checks
from tradebot.backtest import run_backtest
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, ConfigError, MT5Login, RiskParams, StrategyParams
from tradebot.data import load_bars
from tradebot.engine import Engine
from tradebot.live import preview_features
from tradebot.plan import trading_plan
from tradebot.strategy import _trigger, compute_signals, measured_move

P = StrategyParams(name="breakout", breakout_period=5, breakout_min_atr=0.25)


# --- the measured move ----------------------------------------------------------------------------


def test_measured_move_projects_the_range_height_from_the_broken_level():
    df = frame(BREAK_UP)  # quiet range 1.0990 to 1.1010, then a close above it
    target = measured_move(df, P, _trigger(df, P))
    assert target.iloc[-1] == pytest.approx(1.1010 + 0.0020)
    assert target.iloc[:-1].isna().all()
    down = frame((1.0998, 1.0999, 1.0978, 1.0980))
    assert measured_move(down, P, _trigger(down, P)).iloc[-1] == pytest.approx(1.0990 - 0.0020)


def test_target_price_exists_only_on_breakout_bars_and_is_causal(bars):
    p = StrategyParams(name="breakout", breakout_target="measured")
    full = compute_signals(bars, p, "M15")
    assert (full["target_price"].notna() == (full["raw_signal"] != 0)).all()
    ups = full[full["raw_signal"] > 0]
    assert (ups["target_price"] > ups["close"]).mean() > 0.9  # the projection is beyond the break
    prefix = compute_signals(bars.iloc[:3000], p, "M15")
    pd.testing.assert_series_equal(prefix["target_price"], full["target_price"].iloc[:3000])
    assert "target_price" not in compute_signals(bars, StrategyParams(name="breakout"), "M15").columns


def measured_config(config: BotConfig, **risk) -> BotConfig:
    return replace(
        config.with_strategy(name="breakout", breakout_target="measured"), risk=replace(config.risk, **risk)
    )


def test_engine_places_the_target_at_the_measured_price(config, eurusd):
    config = measured_config(config)
    sim = make_sim({"EURUSD": eurusd})
    engine = Engine(config, sim, execute=True)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1, target_price=1.1040))
    (position,) = sim.positions(config.magic)
    assert position.tp == pytest.approx(1.1040)  # the price itself, wherever the fill was
    assert position.price_open - position.sl == pytest.approx(0.0015)  # the stop is still 1.5 ATR

    sim = make_sim({"EURUSD": eurusd})
    engine = Engine(config, sim, execute=True)
    step(engine, sim, config, eurusd, flat(0, signal=-1, raw_signal=-1, target_price=1.0965))
    assert sim.positions(config.magic)[0].tp == pytest.approx(1.0965)


def test_a_target_for_the_other_side_is_ignored(config, eurusd):
    """The advisor prices both sides of every bar; a sell's measured target must not move a buy's."""
    config = measured_config(config)
    sim = make_sim({"EURUSD": eurusd})
    engine = Engine(config, sim, execute=False)
    row = pd.Series(flat(0, raw_signal=-1, target_price=1.0965))
    step(engine, sim, config, eurusd, dict(row))
    buy = engine.plan_entry("EURUSD", eurusd, config.for_symbol("EURUSD"), row, 0.0001, 1)
    sell = engine.plan_entry("EURUSD", eurusd, config.for_symbol("EURUSD"), row, 0.0001, -1)
    assert buy.tp_distance == pytest.approx(0.0030)  # the usual 3 ATR
    assert sell.tp_distance == pytest.approx(1.1000 - 1.0965)


def test_a_target_too_close_for_the_risk_is_skipped(config, eurusd):
    config = measured_config(config, min_reward_risk=1.5)
    sim = make_sim({"EURUSD": eurusd})
    engine = Engine(config, sim, execute=True)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1, target_price=1.1012))  # 11 pips for 15
    assert not sim.positions(config.magic) and engine.skips["reward"] == 1
    assert "0.7 times the risk, below the minimum 1.5" in engine.events[-1].detail
    step(engine, sim, config, eurusd, flat(15, signal=1, raw_signal=1, target_price=1.1040))  # 39 pips
    assert len(sim.positions(config.magic)) == 1


def test_config_checks_the_new_settings():
    with pytest.raises(ConfigError, match="breakout_target"):
        BotConfig(strategy=StrategyParams(breakout_target="pattern")).validate()
    with pytest.raises(ConfigError, match="volume_period"):
        BotConfig(strategy=StrategyParams(volume_min_ratio=0)).validate()
    with pytest.raises(ConfigError, match="min_reward_risk is above tp_atr / sl_atr"):
        BotConfig(risk=RiskParams(min_reward_risk=3.0)).validate()  # ATR targets are always 2.0
    BotConfig(risk=RiskParams(min_reward_risk=2.0)).validate()
    measured_config(BotConfig(), min_reward_risk=3.0).validate()  # a measured target can be any ratio


# --- volume confirmation --------------------------------------------------------------------------


def with_volume(bars: pd.DataFrame, on_breaks: float) -> pd.DataFrame:
    """Flat tick volume of 100, multiplied by ``on_breaks`` on the bars where a breakout triggers."""
    raw = compute_signals(bars, StrategyParams(name="breakout"), "M15")["raw_signal"].to_numpy()
    return bars.assign(tick_volume=np.where(raw != 0, 100 * on_breaks, 100.0))


def test_volume_ratio_compares_a_bar_with_the_bars_before_it(bars):
    df = bars.assign(tick_volume=100.0)
    df.loc[500, "tick_volume"] = 250.0
    out = compute_signals(df, StrategyParams(name="breakout"), "M15")
    assert out.loc[500, "volume_ratio"] == pytest.approx(2.5)  # its own volume is not in the average
    assert out.loc[501, "volume_ratio"] == pytest.approx(100 / ((19 * 100 + 250) / 20))
    assert out["volume_ratio"].iloc[:20].isna().all()
    assert "volume_ratio" not in compute_signals(bars, StrategyParams(name="breakout"), "M15").columns


def test_volume_filter_blocks_quiet_breakouts_only(bars):
    plain = compute_signals(bars, StrategyParams(name="breakout"), "M15")
    p = StrategyParams(name="breakout", use_volume_filter=True)
    loud = compute_signals(with_volume(bars, 2.0), p, "M15")
    quiet = compute_signals(with_volume(bars, 1.2), p, "M15")
    taken = plain["signal"] != 0
    assert taken.sum() > 10
    assert (loud["signal"] == plain["signal"]).all()  # twice the average passes the 1.5 minimum
    assert (quiet["signal"] == 0).all() and (quiet.loc[taken, "block"] == "volume").all()
    assert (quiet["raw_signal"] == plain["raw_signal"]).all()  # the trigger itself is untouched


def test_volume_filter_needs_volume_and_belongs_to_the_breakout_only(bars):
    with pytest.raises(ValueError, match="tradebot fetch"):
        compute_signals(bars, StrategyParams(name="breakout", use_volume_filter=True), "M15")
    for name in ("ema_cross", "pullback", "retest"):
        compute_signals(bars, StrategyParams(name=name, use_volume_filter=True), "M15")  # not used: no error


def test_checklist_and_plan_show_the_volume_rule():
    p = StrategyParams(name="breakout", use_volume_filter=True)
    frame_ = features()
    frame_["volume_ratio"] = 1.0
    frame_.loc[frame_.index[-1], "volume_ratio"] = 1.84
    check = next(c for c in filter_checks(frame_, p, 1) if c.name == "Volume")
    assert check.ok and check.detail == "1.8 times the 20-bar average, needs ≥ 1.5"
    frame_.loc[frame_.index[-1], "volume_ratio"] = 0.9
    assert not next(c for c in filter_checks(frame_, p, 1) if c.name == "Volume").ok
    assert not any(c.name == "Volume" for c in filter_checks(frame_, StrategyParams(name="breakout"), 1))

    config = measured_config(BotConfig().with_strategy(use_volume_filter=True), min_reward_risk=1.5)
    sections = {s.title: "\n".join(s.lines) for s in trading_plan(config)}
    assert "at least 1.5 times the average tick volume of the previous 20 bars" in sections["Entry"]
    assert "Target: the measured move. The height of the 20-bar range" in sections["Exit"]
    assert "closer than 1.5 times its risk is skipped" in sections["Exit"]
    plain = {s.title: "\n".join(s.lines) for s in trading_plan(BotConfig().with_strategy(name="breakout"))}
    assert "Volume" not in plain["Entry"] and "Target: 3 ATR" in plain["Exit"]


# --- the data carries volume ----------------------------------------------------------------------


def test_history_files_keep_tick_volume_when_they_have_it(tmp_path, bars):
    bars.head(50).assign(tick_volume=7).to_csv(tmp_path / "with.csv", index=False)
    bars.head(50).to_csv(tmp_path / "without.csv", index=False)
    assert load_bars(tmp_path / "with.csv")["tick_volume"].tolist() == [7] * 50
    assert "tick_volume" not in load_bars(tmp_path / "without.csv").columns


def test_live_bars_and_the_forming_bar_carry_tick_volume():
    fake = FakeMT5(now=datetime(2026, 3, 4, 12, 7))
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    closed = broker.bars(SYMBOL, "M15", 300)
    assert closed["tick_volume"].tolist() == [100] * 300
    assert broker.forming_bar(SYMBOL, "M15")["tick_volume"] == 100.0
    config = BotConfig(symbols=(SYMBOL,), timeframe="M15").with_strategy(
        name="breakout", use_volume_filter=True
    )
    preview = preview_features(broker, config, SYMBOL, closed)
    assert len(preview) == 301 and preview["volume_ratio"].iloc[-1] == pytest.approx(1.0)


def test_backtest_runs_with_all_three(bars, eurusd):
    config = replace(
        BotConfig(symbols=("EURUSD",), timeframe="M15").with_strategy(
            name="breakout", breakout_target="measured", use_volume_filter=True, volume_min_ratio=1.5
        ),
        risk=RiskParams(min_reward_risk=1.5),
    )
    plain = replace(config, strategy=StrategyParams(name="breakout"), risk=RiskParams())
    data = {"EURUSD": with_volume(bars, 2.0)}
    result = run_backtest(config, data, {"EURUSD": eurusd})
    reference = run_backtest(plain, data, {"EURUSD": eurusd})
    assert result.metrics.trades > 10 and result.signals == reference.signals
    hit = [t for t in result.trades if t.exit_reason.startswith("target")]
    usual = [t for t in reference.trades if t.exit_reason.startswith("target")]
    assert hit and usual
    # a measured target is a different distance from the usual 3 ATR, so the wins differ in size
    assert {round(t.r_multiple, 1) for t in hit} != {round(t.r_multiple, 1) for t in usual}
