"""The reversal warning for a position that is already open."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import SYMBOL, features, primed
from test_alerts import Inbox

from tradebot.broker.base import Position
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams, StrategyParams
from tradebot.data import preset_spec
from tradebot.engine import Engine
from tradebot.live import LiveRunner
from tradebot.plan import trading_plan
from tradebot.reversal import EXIT, WARN, Alerts, Watch, assess, holding_text, watch_position
from tradebot.strategy import compute_signals
from tradebot.ui.monitor import Monitor

NOW = datetime(2026, 3, 4, 12, 7)
OPENED = datetime(2026, 3, 4, 10, 0)  # eight M15 bars of ``features()`` close after this
P = StrategyParams()
SPEC = preset_spec("EURUSD")


def row(minute: int = 0, close: float = 1.1000, **changes) -> dict:
    """One closed bar with nothing against a buy: averages up, trend up, range far away."""
    base = {"time": pd.Timestamp(2026, 3, 4, 10) + pd.Timedelta(minutes=minute), "close": close,
            "ema_fast": 1.1005, "ema_slow": 1.1000, "trend": 1, "signal": 0, "raw_signal": 0,
            "range_high": 1.1100, "range_low": 1.0900, "failed_side": 0, "failed_level": float("nan")}  # fmt: skip
    return base | changes


def watching(side: int, *rows: dict, entry: dict | None = None) -> Watch:
    watch = Watch(side, entry or row(-15))
    for r in rows:
        watch.update(r)
    return watch


# --- the signs ------------------------------------------------------------------------------------


def test_a_quiet_market_shows_nothing():
    watch = watching(1, row(0), row(15), row(30))
    assert (watch.level, watch.signs, watch.bars) == ("", {}, 3)


def test_a_close_beyond_the_range_is_a_sign_until_price_closes_back():
    watch = watching(1, row(0), row(15, close=1.0890))
    assert watch.level == WARN and watch.signs["structure"].level == 1.0900
    watch.update(row(30, close=1.0880, range_low=1.0850))  # still below the level that broke
    assert watch.signs["structure"].level == 1.0900
    watch.update(row(45, close=1.0905))  # back above it: the sign is taken back
    assert watch.level == ""
    # a sell is judged against the range high
    assert watching(-1, row(0, close=1.1105)).signs["structure"].level == 1.1100


def test_averages_count_only_when_they_cross_after_the_entry():
    down = {"ema_fast": 1.0995, "ema_slow": 1.1000}
    assert watching(1, row(0, **down), entry=row(-15, **down)).level == ""  # already so at the entry
    watch = watching(1, row(0), row(15, **down))
    assert list(watch.signs) == ["average"]
    watch.update(row(30))  # crossed back up
    assert watch.level == ""


def test_the_higher_timeframe_trend_turning_is_a_sign_while_it_points_against():
    watch = watching(1, row(0, trend=0), row(15, trend=-1))
    assert list(watch.signs) == ["trend"]
    watch.update(row(30, trend=0))  # the timeframes disagree again: no longer against the trade
    assert watch.level == ""
    assert watching(1, row(0, trend=-1), entry=row(-15, trend=-1)).level == ""  # a counter-trend entry


def test_a_failed_break_is_a_sign_until_price_recovers_the_level_or_breaks_again():
    failed = row(0, close=1.0995, failed_side=1, failed_level=1.1000)
    assert list(watching(1, failed).signs) == ["failed"]
    assert watching(-1, failed).level == ""  # the break that failed was not this position's
    assert watching(1, failed, row(15, close=1.1002)).level == ""  # closed back above the level
    assert watching(1, failed, row(15, close=1.0996, raw_signal=1)).level == ""  # a new buy trigger


def test_an_opposite_signal_alone_is_the_get_out_level():
    watch = watching(1, row(0, signal=-1, raw_signal=-1))
    assert watch.level == EXIT and list(watch.signs) == ["signal"]
    watch.update(row(15, raw_signal=1))  # a trigger back in the trade's favour takes it back
    assert watch.level == ""
    assert watching(1, row(0, raw_signal=-1)).level == ""  # a trigger the filters blocked is not one


def test_two_signs_together_are_the_get_out_level():
    watch = watching(1, row(0, close=1.0890), row(15, close=1.0890, trend=-1))
    assert watch.level == EXIT and set(watch.signs) == {"structure", "trend"}


def test_missing_columns_and_warmup_values_are_not_evidence():
    bare = {"time": pd.Timestamp(2026, 3, 4, 10), "close": 1.1, "trend": 0, "signal": 0, "raw_signal": 0,
            "ema_fast": float("nan"), "ema_slow": float("nan"), "range_low": float("nan")}  # fmt: skip
    assert watching(1, bare, entry=bare).level == ""


# --- from a features frame ------------------------------------------------------------------------


def frame(**last) -> pd.DataFrame:
    """``features()`` with the range columns; ``last`` overrides the bar that closed at 12:00."""
    df = features()
    df["range_high"], df["range_low"] = 1.1100, 1.0900
    for key, value in last.items():
        df.loc[df.index[-1], key] = value
    return df


def test_only_bars_closed_after_the_entry_count():
    df = frame()
    df.loc[df.index[-12], "trend"] = -1  # two hours before the entry: not news
    df.loc[df.index[-12], "range_low"] = 1.2
    watch = watch_position(df, 1, OPENED, 15)
    assert (watch.level, watch.bars) == ("", 8)
    df.loc[df.index[-3], "range_low"] = 1.2  # after the entry, and the last close is still below it
    assert watch_position(df, 1, OPENED, 15).level == WARN
    assert watch_position(df, 1, datetime(2026, 3, 4, 11, 45), 15).bars == 1  # entered on the 11:45 close
    # a position older than the loaded bars is judged from the first of them
    assert watch_position(df, 1, datetime(2026, 1, 1), 15).bars == len(df) - 1
    assert watch_position(df, 1, pd.Timestamp(OPENED, tz="UTC"), 15).bars == 8


def test_assess_words_each_sign_and_dates_it_at_the_bar_close():
    found = assess(frame(range_low=1.1000, trend=-1), 1, OPENED, P, "M15", SPEC)
    assert (found.level, found.headline) == (EXIT, "Get out: reversal against this buy (2 signs)")
    assert [s["key"] for s in found.signs] == ["structure", "trend"]
    assert found.signs[0]["text"] == "M15 closed below the prior 20-bar low at 1.10000."
    assert found.signs[1]["text"] == f"The {P.trend_timeframe} trend turned down."
    assert found.signs[0]["time"] == datetime(2026, 3, 4, 12, 0)
    sell = assess(frame(signal=1, raw_signal=1), -1, OPENED, P, "M15", SPEC)
    assert sell.signs[0]["text"] == "A buy signal passed every filter. The bot closes its own trades on this."
    assert sell.headline == "Get out: reversal against this sell"
    quiet = assess(frame(), 1, OPENED, P, "M15", SPEC)
    assert (quiet.level, quiet.headline, quiet.signs, quiet.forming) == ("", "", [], [])


def test_the_other_signs_have_their_own_wording():
    df = frame(failed_side=1, failed_level=1.0999, ema_fast=1.0985)
    texts = {s["key"]: s["text"] for s in assess(df, 1, OPENED, P, "M15", SPEC).signs}
    assert texts["failed"] == "The break above 1.09990 failed: M15 closed back below it."
    assert texts["average"] == f"EMA {P.fast_ema} crossed below EMA {P.slow_ema} on M15."


def test_the_forming_bar_is_reported_separately_and_never_as_confirmed():
    df = frame()
    forming = df.iloc[[-1]].assign(time=df["time"].iloc[-1] + pd.Timedelta(minutes=15), close=1.0890)
    preview = pd.concat([df, forming], ignore_index=True)
    found = assess(df, 1, OPENED, P, "M15", SPEC, preview)
    assert (found.level, found.signs) == ("", [])
    assert found.headline == "A reversal sign is forming against this buy"
    assert [s["key"] for s in found.forming] == ["structure"] and found.forming_level == WARN
    assert found.forming[0]["text"] == "M15 would close below the prior 20-bar low at 1.09000."
    assert found.forming_closes_at == datetime(2026, 3, 4, 12, 15)
    # a sign that already stands is not repeated as forming, and a stale preview is ignored
    standing = frame(range_low=1.1000)
    again = pd.concat([standing, forming.assign(range_low=1.1000)], ignore_index=True)
    assert assess(standing, 1, OPENED, P, "M15", SPEC, again).forming == []
    assert assess(df, 1, OPENED, P, "M15", SPEC, df).forming == []


def test_real_signals_carry_the_range_columns_causally(bars):
    full = compute_signals(bars, P, "M15")
    prefix = compute_signals(bars.iloc[:3000], P, "M15")
    for column in ("range_high", "range_low"):
        pd.testing.assert_series_equal(prefix[column], full[column].iloc[:3000])
    at = 500
    assert full["range_low"].iloc[at] == bars["low"].iloc[at - P.breakout_period : at].min()
    watch = watch_position(full, 1, full["time"].iloc[-200].to_pydatetime(), 15)
    assert watch.bars == 200 and watch.level in ("", WARN, EXIT)


# --- alerts ---------------------------------------------------------------------------------------


def test_each_sign_is_announced_once_and_again_if_it_returns():
    alerts = Alerts()
    one = assess(frame(range_low=1.1000), 1, OPENED, P, "M15", SPEC)
    two = assess(frame(range_low=1.1000, trend=-1), 1, OPENED, P, "M15", SPEC)
    none = assess(frame(), 1, OPENED, P, "M15", SPEC)
    assert [s["key"] for s in alerts.new(5, one)[0]] == ["structure"]
    assert alerts.new(5, one) == ([], [])
    assert [s["key"] for s in alerts.new(5, two)[0]] == ["trend"]  # only the new one
    assert alerts.new(5, none) == ([], [])
    assert [s["key"] for s in alerts.new(5, one)[0]] == ["structure"]  # taken back, then returned
    assert [s["key"] for s in alerts.new(6, one)[0]] == ["structure"]  # another position
    alerts.forget({6})
    assert [s["key"] for s in alerts.new(5, one)[0]] == ["structure"]  # closed and forgotten


def test_a_forming_sign_is_announced_once_per_bar():
    df = frame()
    forming = df.iloc[[-1]].assign(time=df["time"].iloc[-1] + pd.Timedelta(minutes=15), close=1.0890)
    found = assess(df, 1, OPENED, P, "M15", SPEC, pd.concat([df, forming], ignore_index=True))
    alerts = Alerts()
    assert [s["key"] for s in alerts.new(5, found)[1]] == ["structure"]
    assert alerts.new(5, found) == ([], [])


def test_holding_text_says_what_is_at_stake():
    position = Position(5, "EURUSD", 1, 0.1, 1.1000, 1.0985, 1.1030, OPENED, initial_sl=1.0985, profit=-4.5)
    assert (
        holding_text(position, SPEC, 1.09955)
        == "BUY 0.1 EURUSD from 1.10000, now 1.09955, -4.50 (-0.3R), stop 1.09850."
    )
    bare = Position(6, "EURUSD", -1, 0.1, 1.1000, 0.0, 0.0, OPENED)
    assert holding_text(bare, SPEC, None) == "SELL 0.1 EURUSD from 1.10000, NO STOP on this position."


# --- dashboard and trading loop -------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeMT5:
    fake = FakeMT5(now=NOW)
    fake.positions = [SimpleNamespace(ticket=14, magic=0, symbol=SYMBOL, type=0, volume=0.10, price_open=1.1002,
                                      sl=1.0980, tp=0.0, time=fake.server_epoch(OPENED), profit=-2.0, comment="")]  # fmt: skip
    return fake


@pytest.fixture
def broker(fake) -> MT5Broker:
    return MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)


@pytest.fixture
def cfg() -> BotConfig:
    return BotConfig(
        symbols=(SYMBOL,),
        timeframe="M15",
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


@pytest.fixture
def no_preview(monkeypatch):
    monkeypatch.setattr("tradebot.ui.monitor.preview_features", lambda *a, **k: None)


def test_dashboard_warns_about_a_manual_position_once(cfg, broker, no_preview):
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), frame(range_low=1.1000))
    for _ in range(3):
        monitor.refresh()
    (message,) = inbox.sent
    assert message.startswith(f"REVERSAL WARNING · {SYMBOL}\nReversal warning on this buy. M15 closed below")
    assert "You hold BUY 0.1 EURUSDm from 1.10020, now 1.10000, -2.00 (-0.1R), stop 1.09800." in message
    state = monitor.snapshot()
    (position,) = state["positions"]
    assert position["bot"] is False and position["reversal"]["level"] == "warn"
    assert position["reversal"]["signs"][0]["time"] == "2026-03-04T12:00:00Z"
    assert position["reversal"]["bars"] == 8
    assert [a["action"] for a in state["activity"]] == ["REVERSAL"]
    json.dumps(state, allow_nan=False)


def test_dashboard_says_get_out_at_two_signs_and_nothing_when_quiet(cfg, broker, no_preview):
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), frame(range_low=1.1000, trend=-1))
    monitor.refresh()
    (message,) = inbox.sent
    assert message.startswith(f"GET OUT · {SYMBOL}\nGet out: reversal against this buy (2 signs).")
    assert [a["action"] for a in monitor.snapshot()["activity"]] == ["EXIT"]

    quiet = Inbox()
    calm = primed(Monitor(cfg, broker, [SYMBOL], notifier=quiet), frame())
    calm.refresh()
    assert quiet.sent == []
    assert calm.snapshot()["positions"][0]["reversal"]["level"] == ""


def test_a_position_on_an_unwatched_symbol_has_no_check(cfg, broker, fake, no_preview):
    fake.positions[0].symbol = "XAUUSDm"
    monitor = primed(Monitor(cfg, broker, [SYMBOL]), frame(range_low=1.1000))
    monitor.refresh()
    assert monitor.snapshot()["positions"][0]["reversal"] is None


def test_closing_the_position_clears_what_was_remembered(cfg, broker, fake, no_preview):
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), frame(range_low=1.1000))
    monitor.refresh()
    held, fake.positions = fake.positions, []
    monitor.refresh()
    assert monitor._watches == {} and monitor.snapshot()["positions"] == []
    fake.positions = held  # the same ticket seen again is announced again
    monitor.refresh()
    assert len(inbox.sent) == 2


def test_trading_loop_warns_once_per_sign(cfg, broker, monkeypatch):
    inbox = Inbox()
    monkeypatch.setattr("tradebot.live.latest_features", lambda *a, **k: frame(range_low=1.1000, trend=-1))
    runner = LiveRunner(cfg, broker, Engine(cfg, broker, execute=False, live=True), [SYMBOL], notifier=inbox)
    runner.cycle()
    runner.cycle()
    (message,) = inbox.sent
    assert message.startswith(f"GET OUT · {SYMBOL}\nGet out: reversal against this buy (2 signs).")
    assert "You hold BUY 0.1 EURUSDm from 1.10020" in message


def test_the_written_plan_describes_the_warning(cfg):
    exits = next(s for s in trading_plan(cfg) if s.title == "Exit")
    (line,) = [text for text in exits.lines if text.startswith("Reversal warning")]
    assert "a M15 close beyond the prior 20-bar extreme" in line
    assert "two signs standing together, is the get-out level" in line


def test_entry_bar_is_the_reference_not_evidence():
    """The bar that closed just before the entry sets the starting state and is never counted."""
    df = frame()
    entry_bar = df.index[-9]  # opens 09:45, closes 10:00 = the entry
    df.loc[entry_bar, ["signal", "raw_signal"]] = [-1, -1]
    assert watch_position(df, 1, OPENED, 15).level == ""
    assert watch_position(df, 1, OPENED - timedelta(minutes=1), 15).level == EXIT
