"""False breakouts: a break has to be decisive to count, and the advisor says when one fails."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import SYMBOL, advice_for, features
from test_alerts import Inbox, primed
from test_retest import BREAK_UP, DRIFT, LEVEL, frame

from tradebot.advisor import failed_break
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, ConfigError, MT5Login, ScheduleParams, StrategyParams
from tradebot.strategy import _trigger, break_failures, compute_signals, retest_setup
from tradebot.ui.monitor import Monitor

NOW = datetime(2026, 3, 4, 12, 7)
P = StrategyParams(name="breakout", breakout_period=5, breakout_min_atr=0.25, retest_window=4)
ANY = StrategyParams(name="breakout", breakout_period=5, breakout_min_atr=0.0, retest_window=4)
POKE = (1.1002, 1.1012, 1.1001, 1.1011)  # closes one pip above the 5-bar high; 0.25 ATR is 2.5 pips
BACK_INSIDE = (1.1018, 1.1019, 1.1000, 1.1004)  # closes six pips below the level
SHALLOW_DIP = (1.1018, 1.1019, 1.1006, 1.1009)  # closes one pip below it: inside the margin


# --- a break has to be decisive -------------------------------------------------------------------


def test_a_marginal_close_through_the_level_is_not_a_breakout():
    assert _trigger(frame(POKE), P).tolist() == [0] * 7
    assert _trigger(frame(POKE), ANY).tolist() == [0] * 6 + [1]  # the old rule: any close through
    assert _trigger(frame(BREAK_UP), P).tolist() == [0] * 6 + [1]  # ten pips through: decisive


def test_sell_breakouts_need_the_same_margin():
    def mirror(o, h, low, c):
        return (2.2 - o, 2.2 - low, 2.2 - h, 2.2 - c)

    assert _trigger(frame(mirror(*POKE)), P).tolist() == [0] * 7
    assert _trigger(frame(mirror(*BREAK_UP)), P).tolist() == [0] * 6 + [-1]


def test_the_first_close_through_must_itself_be_decisive():
    """After a marginal poke the level is already broken, so the next bar is not a fresh break."""
    strong_follow_through = (1.1011, 1.1030, 1.1010, 1.1028)
    assert not _trigger(frame(POKE, strong_follow_through), P).any()


def test_decisive_breakouts_are_a_subset_of_all_breakouts(bars):
    strict = compute_signals(bars, StrategyParams(name="breakout"), "M15")["raw_signal"]
    loose = compute_signals(bars, StrategyParams(name="breakout", breakout_min_atr=0.0), "M15")["raw_signal"]
    assert 0 < (strict != 0).sum() < (loose != 0).sum()
    assert (loose[strict != 0] == strict[strict != 0]).all()


def test_config_rejects_a_negative_margin():
    with pytest.raises(ConfigError, match="breakout_min_atr"):
        BotConfig(symbols=(SYMBOL,), strategy=StrategyParams(breakout_min_atr=-0.1)).validate()


# --- when a break fails ---------------------------------------------------------------------------


def failures(*tail, p=P):
    df = frame(*tail)
    return break_failures(df, p, _trigger(df, p))


def test_a_close_back_through_the_level_marks_the_break_as_failed():
    out = failures(BREAK_UP, DRIFT, BACK_INSIDE)
    assert out["failed_side"].tolist() == [0] * 8 + [1]
    assert out["failed_level"].iloc[-1] == pytest.approx(LEVEL)


def test_a_dip_inside_the_margin_is_not_a_failure_and_the_watch_expires():
    assert not failures(BREAK_UP, SHALLOW_DIP)["failed_side"].any()
    late = failures(BREAK_UP, DRIFT, DRIFT, DRIFT, DRIFT, BACK_INSIDE)  # five bars later; watched for 4
    assert not late["failed_side"].any()


def test_a_break_fails_only_once():
    assert failures(BREAK_UP, BACK_INSIDE, BACK_INSIDE)["failed_side"].tolist() == [0] * 7 + [1, 0]


def test_retest_marks_the_same_failure():
    p = StrategyParams(name="retest", retest_period=5, retest_window=4, retest_tolerance_atr=0.25)
    out = retest_setup(frame(BREAK_UP, BACK_INSIDE), p)
    assert out["failed_side"].tolist() == [0] * 7 + [1]
    assert out["failed_level"].iloc[-1] == pytest.approx(LEVEL)


def test_failures_are_causal_and_only_tracked_for_break_setups(bars):
    p = StrategyParams(name="breakout")
    full = compute_signals(bars, p, "M15")
    assert (full["failed_side"] != 0).sum() > 0
    prefix = compute_signals(bars.iloc[:3000], p, "M15")
    pd.testing.assert_series_equal(prefix["failed_side"], full["failed_side"].iloc[:3000])
    # a failure always follows a trigger of the same side
    for i in full.index[full["failed_side"] != 0][:50]:
        before = full["raw_signal"].iloc[max(0, i - p.retest_window) : i]
        assert (before == full["failed_side"].iloc[i]).any()
    assert "failed_side" not in compute_signals(bars, StrategyParams(), "M15").columns


# --- what the advisor says ------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeMT5:
    return FakeMT5(now=NOW)


@pytest.fixture
def broker(fake) -> MT5Broker:
    return MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)


@pytest.fixture
def cfg() -> BotConfig:
    return BotConfig(
        symbols=(SYMBOL,),
        timeframe="M15",
        strategy=StrategyParams(name="breakout"),
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


def with_failure(bars_ago: int = 0, level: float = 1.0999, **last) -> pd.DataFrame:
    """Closed bars (last close 1.09958) with a buy break of ``level`` that failed ``bars_ago`` bars back."""
    df = features()
    df["failed_side"], df["failed_level"] = 0, float("nan")
    at = df.index[-1 - bars_ago]
    df.loc[at, ["failed_side", "failed_level"]] = [1, level]
    for key, value in last.items():
        df.loc[df.index[-1], key] = value
    return df


def test_failed_break_is_reported_until_a_newer_break_replaces_it():
    found = failed_break(with_failure(bars_ago=2), 15)
    assert (found["side"], found["level"], found["bars_ago"]) == (1, 1.0999, 2)
    assert found["recovered"] is False  # the last close, 1.09958, is still below the level
    assert found["time"] == datetime(2026, 3, 4, 11, 30)  # the close of the bar that failed
    assert failed_break(with_failure(bars_ago=2, raw_signal=-1), 15) is None  # a newer trigger
    assert failed_break(with_failure(bars_ago=12), 15) is None  # too long ago
    assert failed_break(features(), 15) is None  # this setup does not track breaks


def test_advice_leads_with_the_failed_break(cfg, broker):
    a = advice_for(cfg, broker, with_failure())
    assert (a.action, a.actionable) == ("WAIT", False)
    assert a.headline == "Buy break failed on the last bar"
    assert "broke above 1.09990 and has closed back below it" in a.detail
    assert advice_for(cfg, broker, with_failure(bars_ago=3)).headline == "Buy break failed 3 bars ago"
    # a live signal on the last bar still comes first
    assert advice_for(cfg, broker, with_failure(bars_ago=3, signal=-1, raw_signal=-1)).action == "SELL"


def test_a_break_that_recovered_is_not_the_headline(cfg, broker):
    """Price closed back below 1.0995, but the last bar closed above it again: unsettled, not dead."""
    a = advice_for(cfg, broker, with_failure(bars_ago=3, level=1.0995))
    assert a.failed_break["recovered"] is True
    assert a.headline == "No setup yet"


def test_breakout_advice_states_the_margin(cfg, broker):
    a = advice_for(cfg, broker, features())
    assert "plus 0.25 ATR, so a marginal poke through it does not count" in a.trigger
    sell = advice_for(cfg, broker, features({"trend": -1}))
    assert sell.side == -1 and "20-bar low" in sell.trigger and "minus 0.25 ATR" in sell.trigger
    assert sell.trigger_level == pytest.approx(features()["low"].tail(20).min() - 0.00025)
    loose = advice_for(cfg.with_strategy(breakout_min_atr=0.0), broker, features())
    assert "plus" not in loose.trigger and loose.trigger_level == pytest.approx(
        features()["high"].tail(20).max()
    )


def test_a_failed_break_is_announced_once(cfg, broker):
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), with_failure())
    for _ in range(3):
        monitor.refresh()
    (message,) = inbox.sent
    assert message.startswith(f"BREAK FAILED · {SYMBOL}\nThe buy break above 1.09990 has failed")
    assert "closed at 12:00 UTC finished back below it" in message
    state = monitor.snapshot()
    assert [a["action"] for a in state["activity"]] == ["FAILED"]
    assert state["symbols"][0]["advice"]["failed_break"]["bars_ago"] == 0
    json.dumps(state, allow_nan=False)


def test_an_old_failure_is_shown_but_not_announced(cfg, broker):
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), with_failure(bars_ago=4))
    monitor.refresh()
    assert inbox.sent == []
    assert monitor.snapshot()["symbols"][0]["advice"]["headline"] == "Buy break failed 4 bars ago"
