"""The assessment is independent of trading permissions and safely reaches the dashboard."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL, features

from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams
from tradebot.ui.monitor import LOST_LINK, Monitor


@pytest.fixture
def desk():
    fake = FakeMT5(now=NOW)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    config = BotConfig(
        symbols=(SYMBOL,),
        timeframe="M15",
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )
    messages = []
    monitor = Monitor(
        config,
        broker,
        [SYMBOL],
        notifier=SimpleNamespace(send=lambda text, kind="info": messages.append(text)),
    )
    monitor._features[SYMBOL] = features()
    monitor._fetched[SYMBOL] = time.monotonic()
    return monitor, fake, messages


def assessment(monitor):
    return monitor.snapshot()["symbols"][0]["analysis"]


def test_assessment_visible_without_order_permissions_or_manual_book(desk):
    monitor, fake, _ = desk
    monitor.refresh()
    state = monitor.snapshot()
    assert not state["gate"]["execute"] and state["book"] is None
    assert assessment(monitor)["status"] == "ready"
    assert len(assessment(monitor)["scenarios"]) == 2
    assert assessment(monitor)["change"] is None
    json.dumps(state, allow_nan=False)
    text = monitor.analysis_text(SYMBOL)
    assert "Bullish scenario" in text and "Bearish scenario" in text and "Source:" in text
    assert fake.sent == []


def test_account_equity_and_gate_do_not_change_market_assessment(desk):
    monitor, fake, _ = desk
    monitor.refresh()
    original = assessment(monitor)
    account = fake.account_info()
    account.equity, account.balance, account.trade_mode = 25.0, 25.0, fake.ACCOUNT_TRADE_MODE_REAL
    fake.account_info = lambda: account
    monitor.refresh()
    assert assessment(monitor) == original
    assert fake.sent == []


def test_changed_interpretation_alerts_once_not_on_every_quote(desk):
    monitor, fake, messages = desk
    monitor.refresh()
    assert not any(message.startswith("Analysis changed") for message in messages)
    monitor.refresh()
    frame = monitor._features[SYMBOL].copy()
    frame.loc[frame.index[-1], "macd_hist"] = -0.0002
    monitor._features[SYMBOL] = frame
    monitor.refresh()
    changed = assessment(monitor)["change"]
    assert changed and assessment(monitor)["regime"] == "pullback"
    fake.utc_now += timedelta(seconds=1)
    monitor.refresh()
    assert assessment(monitor)["change"] == changed
    assert len([message for message in messages if message.startswith("Analysis changed")]) == 1
    assert len([event for event in monitor.activity if event["action"] == "ANALYSIS"]) == 1
    assert fake.sent == []


def test_connection_loss_clears_live_scenarios_without_mutating_prior_snapshot(desk):
    monitor, _, _ = desk
    monitor.refresh()
    before = assessment(monitor)
    monitor._report(LOST_LINK)
    assert before["status"] == "ready" and before["scenarios"]
    after = assessment(monitor)
    assert after["status"] == "stale" and after["scenarios"] == [] and after["bias"] == 0
    assert "unavailable" in monitor.analysis_text(SYMBOL)


def test_phone_request_rechecks_age_and_never_places_an_order(desk):
    monitor, fake, _ = desk
    monitor.refresh()
    fake.utc_now += timedelta(minutes=3)
    assert "stale" in monitor.analysis_text(SYMBOL)
    # Execute the queued read in-process, as the worker does; no Telegram/network transport.
    monitor.call = lambda fn, timeout=20: fn()
    assert "stale" in monitor.command("/analysis EURUSD")
    assert monitor.command("/analysis") == "Use: /analysis SYMBOL"
    assert fake.sent == []


def test_ticks_received_during_refresh_are_not_mistaken_for_future_data(desk):
    monitor, fake, _ = desk
    read = fake.symbol_info_tick

    def arriving_tick(symbol):
        fake.utc_now += timedelta(seconds=1)
        return read(symbol)

    fake.symbol_info_tick = arriving_tick
    monitor.refresh()
    assert assessment(monitor)["status"] == "ready"
    assert monitor.snapshot()["now"] > NOW.isoformat() + "Z"
    assert fake.sent == []


def test_a_broker_clock_a_few_seconds_ahead_is_not_data_from_the_future(desk):
    """Regression: with the server clock 3 s fast every fresh tick was 'dated in the future'."""
    monitor, fake, _ = desk
    read = fake.symbol_info_tick

    def fast_clock(symbol):
        tick = read(symbol)
        return SimpleNamespace(time=tick.time + 3, bid=tick.bid, ask=tick.ask)

    fake.symbol_info_tick = fast_clock
    monitor.refresh()
    assert assessment(monitor)["status"] == "ready"
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(time=read(symbol).time + 600, bid=1.1, ask=1.10012)
    monitor.refresh()
    assert assessment(monitor)["status"] == "incomplete"  # ten minutes ahead is a clock fault


def test_a_pause_and_resume_sends_no_alert(desk):
    """Regression: every weekend close and reopen sent 'Analysis changed' for each symbol."""
    monitor, fake, messages = desk
    monitor.refresh()
    fake.utc_now += timedelta(hours=3)  # the bars stop: stale
    monitor.refresh()
    assert assessment(monitor)["status"] == "stale"
    fake.utc_now -= timedelta(hours=3)  # fresh again, and the reading has not changed
    monitor.refresh()
    assert assessment(monitor)["status"] == "ready"
    assert not any(message.startswith("Analysis changed") for message in messages)
    assert not any(event["action"] == "ANALYSIS" for event in monitor.activity)
