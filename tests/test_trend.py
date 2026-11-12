"""The trend on the trading timeframe, its warning states, and every setup watched at once."""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import SYMBOL, features, primed
from test_alerts import Inbox

from tradebot.broker.mt5 import MT5Broker
from tradebot.config import STRATEGIES, BotConfig, MT5Login, ScheduleParams, StrategyParams
from tradebot.data import preset_spec
from tradebot.strategy import compute_signals
from tradebot.trend import SIGNS, TrendAlerts, describe, trend_frame
from tradebot.ui.monitor import Monitor

NOW = datetime(2026, 3, 4, 12, 7)
P = StrategyParams()
SPEC = preset_spec("EURUSD")


def market(n: int = 40, **columns) -> pd.DataFrame:
    """A calm uptrend: averages one ATR apart, ADX 30, momentum up. ``columns`` replace whole
    columns (a list for the last bars, or one value for all of them)."""
    df = pd.DataFrame({
        "time": pd.date_range("2026-03-02", periods=n, freq="15min"),
        "close": 1.1010, "atr": 0.0010, "ema_fast": 1.1010, "ema_slow": 1.1000,
        "adx": 30.0, "macd_hist": 0.0002, "trend": 1,
    })  # fmt: skip
    for key, value in columns.items():
        if isinstance(value, list):
            df.loc[df.index[-len(value) :], key] = value
        else:
            df[key] = value
    return df


def last(df: pd.DataFrame) -> pd.Series:
    return trend_frame(df, P).iloc[-1]


# --- the frame ------------------------------------------------------------------------------------


def test_direction_and_age_follow_the_averages():
    frame = trend_frame(market(), P)
    assert (frame["direction"] == 1).all() and frame["age"].tolist() == list(range(1, 41))
    assert (frame["state"] == "intact").all() and (frame["level"] == 0).all()
    crossed = trend_frame(market(ema_fast=[1.0990] * 5), P)
    assert crossed["direction"].iloc[-6:].tolist() == [1, -1, -1, -1, -1, -1]
    assert crossed["age"].iloc[-6:].tolist() == [35, 1, 2, 3, 4, 5]


def test_no_trend_while_the_averages_warm_up():
    frame = trend_frame(market(ema_slow=np.nan), P)
    assert (
        (frame["direction"] == 0).all() and (frame["state"] == "none").all() and (frame["level"] == 0).all()
    )


def test_price_through_the_slow_average_is_the_ending_state():
    row = last(market(close=[1.0995]))
    assert bool(row["price"]) and row["state"] == "ending"
    down = market(ema_fast=1.0990, macd_hist=-0.0002, close=1.0990)
    assert last(down)["state"] == "intact"
    assert last(down.assign(close=[*[1.0990] * 39, 1.1004]))["state"] == "ending"


def test_converging_averages_are_the_weakening_state_while_they_keep_narrowing():
    narrowing = market(ema_fast=[1.1007, 1.1005, 1.1004])  # gap 1.0 ATR at its widest, now 0.4
    row = last(narrowing)
    assert bool(row["converging"]) and row["state"] == "weakening"
    assert row["gap"] == pytest.approx(0.4) and row["gap_peak"] == pytest.approx(1.0)
    widening = market(ema_fast=[1.1005, 1.1003, 1.1004])  # still under half, but opening again
    assert not last(widening)["converging"]
    young = market(ema_fast=1.1003, close=1.1003)  # never got 0.5 ATR apart: nothing to converge from
    assert not last(young.assign(ema_fast=[*[1.1003] * 39, 1.1001]))["converging"]


def test_fading_strength_needs_a_real_peak():
    assert bool(last(market(adx=[*[40.0] * 39, 31.0]))["fading"])  # 22% below a high of 40
    assert not last(market(adx=[*[40.0] * 39, 33.0]))["fading"]
    assert not last(market(adx=[*[24.0] * 39, 15.0]))["fading"]  # the trend never reached 25


def test_momentum_and_divergence():
    assert bool(last(market(macd_hist=[-0.0001]))["momentum"])
    # two new 20-bar closing highs, six bars apart, the second with weaker momentum
    close = [1.1010] * 30 + [1.1020] + [1.1015] * 5 + [1.1025] + [1.1020] * 3
    hist = [0.0002] * 30 + [0.0005] + [0.0002] * 5 + [0.0003] + [0.0002] * 3
    frame = trend_frame(market(close=close, macd_hist=hist), P)
    assert not frame["divergence"].iloc[30] and frame["divergence"].iloc[36:].all()
    stronger = [*hist[:36], 0.0006, *hist[37:]]
    assert not trend_frame(market(close=close, macd_hist=stronger), P)["divergence"].any()
    long = market(n=60, close=close + [1.1020] * 20, macd_hist=hist + [0.0002] * 20)
    flags = trend_frame(long, P)["divergence"]
    assert flags.iloc[46] and not flags.iloc[47]  # it stands for ten bars


def test_the_three_unreliable_signs_do_not_change_the_state():
    """Measured: divergence, momentum and fading alone did not come before an ending more often."""
    row = last(market(adx=[*[40.0] * 39, 30.0], macd_hist=[-0.0001]))
    assert row["level"] == 2 and row["state"] == "intact"


def test_the_frame_is_causal_on_real_signals(bars):
    features_ = compute_signals(bars, P, "M15")
    full, prefix = trend_frame(features_, P), trend_frame(features_.iloc[:3000], P)
    pd.testing.assert_frame_equal(prefix, full.iloc[:3000])
    assert set(full["state"]) == {"none", "intact", "weakening", "ending"}
    assert (full.loc[full["state"] == "ending", "price"]).all()
    assert full[list(SIGNS)].any().all()  # every sign occurs somewhere in real data


# --- in words -------------------------------------------------------------------------------------


def test_describe_an_intact_trend():
    found = describe(market(), P, "M15", SPEC, 15)
    assert (found["direction"], found["age"], found["state"], found["level"]) == (1, 40, "intact", 0)
    assert found["headline"] == "M15 uptrend, 40 bars old, steady"
    assert found["since"] == datetime(2026, 3, 2, 0, 15)  # the close of the first bar
    assert found["higher"] == [{"timeframe": P.trend_timeframe, "direction": 1}]
    assert found["signs"] == [] and found["previous"] is None


def test_describe_names_the_signs_and_which_ones_lead():
    found = describe(market(close=[1.0995], macd_hist=[-0.0001]), P, "M15", SPEC, 15)
    assert found["state"] == "ending" and found["headline"] == "M15 uptrend is likely ending (40 bars old)"
    assert [(s["key"], s["leading"]) for s in found["signs"]] == [("price", True), ("momentum", False)]
    assert found["signs"][0]["text"] == f"Price closed below EMA {P.slow_ema} (1.10000)."
    weak = describe(market(ema_fast=[1.1007, 1.1005, 1.1004]), P, "M15", SPEC, 15)
    assert weak["headline"] == "M15 uptrend is weakening (40 bars old)"
    assert weak["signs"][0]["text"] == (
        f"The averages are closing in: EMA {P.fast_ema} is 0.4 ATR from EMA {P.slow_ema}, down from 1.0 at its widest."
    )
    fading = describe(market(adx=[*[40.0] * 39, 30.0]), P, "M15", SPEC, 15)
    assert fading["signs"][0]["text"] == "Strength is fading: ADX 30, down from a high of 40 in this trend."
    assert fading["headline"] == "M15 uptrend, 40 bars old, steady"


def test_describe_a_new_trend_and_the_one_before_it():
    found = describe(market(ema_fast=[1.0990], macd_hist=[-0.0002], close=[1.0990]), P, "M15", SPEC, 15)
    assert found["headline"] == f"New M15 downtrend: EMA {P.fast_ema} crossed below EMA {P.slow_ema}"
    assert (found["direction"], found["age"]) == (-1, 1) and found["previous"] == {"direction": 1, "age": 39}
    assert found["since"] == datetime(2026, 3, 2, 10, 0)
    warming = describe(market(ema_slow=np.nan), P, "M15", SPEC, 15)
    assert (warming["direction"], warming["state"], warming["since"]) == (0, "none", None)
    assert warming["strength"] == "" and "warming up" in warming["headline"]


# --- alerts ---------------------------------------------------------------------------------------


def test_trend_alerts_say_each_step_once():
    alerts = TrendAlerts()
    intact = describe(market(), P, "M15", SPEC, 15)
    weak = describe(market(ema_fast=[1.1007, 1.1005, 1.1004]), P, "M15", SPEC, 15)
    ending = describe(market(close=[1.0995]), P, "M15", SPEC, 15)
    assert alerts.new("EURUSD", intact) == ""  # first look, nothing worth saying
    assert alerts.new("EURUSD", weak) == "weakening"
    assert alerts.new("EURUSD", weak) == ""
    assert alerts.new("EURUSD", intact) == "" and alerts.new("EURUSD", weak) == ""  # eased and came back
    assert alerts.new("EURUSD", ending) == "ending"
    assert alerts.new("EURUSD", ending) == ""
    fresh = describe(market(ema_fast=[1.0990], macd_hist=[-0.0002], close=[1.0990]), P, "M15", SPEC, 15)
    assert alerts.new("EURUSD", fresh) == "start"
    assert alerts.new("EURUSD", fresh) == ""
    assert alerts.new("GBPUSD", ending) == "ending"  # first look at another symbol, already ending
    assert alerts.new("USDJPY", describe(market(ema_slow=np.nan), P, "M15", SPEC, 15)) == ""
    alerts.clear()
    assert alerts.new("EURUSD", fresh) == "start"


def test_a_trend_first_seen_mid_way_is_a_start_only_when_it_replaces_another():
    alerts = TrendAlerts()
    up = describe(market(), P, "M15", SPEC, 15)
    assert alerts.new("EURUSD", up) == ""
    older = describe(market(ema_fast=[1.0990] * 3, macd_hist=-0.0002, close=1.0990), P, "M15", SPEC, 15)
    assert older["age"] == 3 and alerts.new("EURUSD", older) == "start"  # missed bars: still news


# --- dashboard ------------------------------------------------------------------------------------


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
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


@pytest.fixture
def breakout_buys(monkeypatch):
    """The other setups are computed from the bars: make the breakout one signal a buy."""
    monkeypatch.setattr("tradebot.ui.monitor.preview_features", lambda *a, **k: None)

    def compute(bars, p, timeframe, htf=None):
        return features({"signal": 1, "raw_signal": 1} if p.name == "breakout" else None)

    monkeypatch.setattr("tradebot.ui.monitor.compute_signals", compute)


def test_dashboard_reports_every_setup_and_the_trend(cfg, broker, breakout_buys):
    monitor = primed(Monitor(cfg, broker, [SYMBOL]), features())
    monitor.refresh()
    state = monitor.snapshot()
    row = state["symbols"][0]
    assert [s["name"] for s in row["setups"]] == list(STRATEGIES)
    by_name = {s["name"]: s for s in row["setups"]}
    assert by_name["ema_cross"]["active"] and by_name["ema_cross"]["action"] == row["advice"]["action"]
    assert (by_name["breakout"]["action"], by_name["breakout"]["side"]) == ("BUY", 1)
    assert by_name["breakout"]["text"].startswith("Buy signal: 20-bar high breakout")
    assert by_name["pullback"]["action"] == "WAIT" and "RSI(3)" in by_name["pullback"]["text"]
    assert by_name["retest"]["label"] == "Break and retest"
    assert row["trend"]["direction"] == 1 and row["trend"]["headline"] == "M15 uptrend, 30 bars old, steady"
    json.dumps(state, allow_nan=False)


def test_a_signal_from_another_setup_is_announced_once_with_its_plan(cfg, broker, breakout_buys):
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), features())
    for _ in range(3):
        monitor.refresh()
    (message,) = inbox.sent
    assert message.startswith(f"BUY · {SYMBOL} · Breakout setup\nBuy {SYMBOL} now.")
    assert "BUY plan: entry ~1.10012" in message
    (entry,) = monitor.snapshot()["activity"]
    assert entry["action"] == "BUY" and entry["text"].startswith(f"Breakout setup: Buy {SYMBOL} now.")
    # switching the page to that setup does not announce the same signal again
    assert monitor._apply_strategy("breakout")["ok"]
    primed(monitor, features({"signal": 1, "raw_signal": 1}))
    monitor.refresh()
    assert len(inbox.sent) == 1
    assert monitor.snapshot()["symbols"][0]["advice"]["action"] == "BUY"


def test_a_signal_another_setup_cannot_take_is_a_skip(cfg, broker, breakout_buys):
    closed = BotConfig(symbols=(SYMBOL,), timeframe="M15",
                       schedule=ScheduleParams(session="custom", start_hour_utc=1, end_hour_utc=2, news_file=""))  # fmt: skip
    monitor = primed(Monitor(closed, broker, [SYMBOL]), features())
    monitor.refresh()
    breakout = next(s for s in monitor.snapshot()["symbols"][0]["setups"] if s["name"] == "breakout")
    assert breakout["action"] == "SKIP" and "session" in breakout["text"]


def test_dashboard_announces_a_trend_that_is_likely_ending_once(cfg, broker, breakout_buys):
    inbox = Inbox()
    frame = features()
    frame["close"] = frame["ema_slow"] - 0.0002  # every close below the slow average, in an uptrend
    monitor = primed(Monitor(cfg.with_strategy(name="breakout"), broker, [SYMBOL], notifier=inbox), frame)
    for _ in range(3):
        monitor.refresh()
    trend = [m for m in inbox.sent if m.startswith("TREND")]
    assert len(trend) == 1 and trend[0].startswith(f"TREND ENDING · {SYMBOL}\nM15 uptrend is likely ending")
    assert "not which way price goes next" in trend[0]
    assert monitor.snapshot()["symbols"][0]["trend"]["state"] == "ending"
    assert "ENDING" in [a["action"] for a in monitor.snapshot()["activity"]]


def test_an_intact_trend_is_shown_but_not_announced(cfg, broker, monkeypatch):
    monkeypatch.setattr("tradebot.ui.monitor.preview_features", lambda *a, **k: None)
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), features())
    monitor.refresh()
    assert inbox.sent == [] and monitor.snapshot()["symbols"][0]["trend"]["state"] == "intact"
    assert len(monitor.snapshot()["symbols"][0]["setups"]) == len(STRATEGIES)  # computed from the real bars
