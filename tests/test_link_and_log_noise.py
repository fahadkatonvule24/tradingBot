"""Audit of 24 Nov 2026: a dropped internet connection, and log lines that hid real problems.

MT5's journal on the user's PC showed 33 drops of the broker connection in one night. Each one
logged "lost the connection to MetaTrader 5: Success (1). Is the terminal open?", rebuilt the
link to a terminal that was fine, and once stopped orders with "algo trading is disabled" (and
a phone alert) for 15 seconds. Overnight every stock logged two lines every 15 s."""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace

from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL
from test_alerts import Inbox

from tradebot import cli
from tradebot.broker.mt5 import MT5Broker, MT5Error
from tradebot.config import BotConfig, MT5Login, ScheduleParams
from tradebot.engine import Engine
from tradebot.live import Link, LiveRunner, trading_gate

SCHEDULE = ScheduleParams(session="all", trade_weekends=True, news_file="")


def offline(fake: FakeMT5) -> None:
    """The terminal answers; its connection to the broker's server is down."""
    fake.terminal_info = lambda: SimpleNamespace(trade_allowed=True, connected=False)


def online(fake: FakeMT5) -> None:
    del fake.terminal_info


def test_a_broker_drop_is_a_warning_that_says_so_and_leaves_the_terminal_link_alone(caplog):
    fake = FakeMT5(now=NOW)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    tries = []
    broker.reconnect = lambda: tries.append(1) or True
    now = [0.0]
    link = Link(broker, clock=lambda: now[0])
    offline(fake)
    with caplog.at_level("INFO", logger="tradebot.live"):
        for second in range(40):
            now[0] = float(second)
            assert not link.ok() and link.offline
        assert tries == []  # MT5 reconnects to its server by itself: no shutdown/initialize churn
        (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert record.levelname == "WARNING" and "broker's server" in record.getMessage()
        assert "Success" not in caplog.text and "Is the terminal open" not in caplog.text
        online(fake)
        now[0] = 41.0
        assert link.ok() and not link.offline
        assert "reconnected to MetaTrader 5 after 41 s" in caplog.text


def test_a_drop_during_a_cycle_does_not_stop_orders_or_alert_the_phone(tmp_path):
    fake = FakeMT5(now=NOW)
    config = BotConfig(
        symbols=(SYMBOL,), timeframe="M15", dry_run=False, live_trading_confirm=True, schedule=SCHEDULE
    )
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    engine = Engine(config, broker, execute=True, live=True)
    inbox = Inbox()
    runner = LiveRunner(
        config, broker, engine, [SYMBOL], notifier=inbox, manage=True,
        gate=lambda: trading_gate(config, broker.account(), True, broker.terminal_trading_allowed()),
    )  # fmt: skip
    seen = []
    runner._process = lambda symbol: seen.append(engine.execute)
    original = runner._link.ok
    runner._link.ok = lambda: original() or True  # the link was fine when the cycle began...
    offline(fake)  # ...and dropped before the first symbol
    runner.cycle()
    assert seen == [] and engine.execute and runner.manage
    assert not any("Orders stopped" in m for m in inbox.sent)
    online(fake)
    runner._link.ok = original
    runner.cycle()
    assert seen == [True]


def test_algo_trading_switched_off_while_connected_still_stops_orders():
    fake = FakeMT5(now=NOW)
    config = BotConfig(
        symbols=(SYMBOL,), timeframe="M15", dry_run=False, live_trading_confirm=True, schedule=SCHEDULE
    )
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    engine = Engine(config, broker, execute=True, live=True)
    runner = LiveRunner(
        config, broker, engine, [SYMBOL], manage=True,
        gate=lambda: trading_gate(config, broker.account(), True, broker.terminal_trading_allowed()),
    )  # fmt: skip
    runner._process = lambda symbol: None
    fake.terminal_info = lambda: SimpleNamespace(trade_allowed=False, connected=True)
    runner.cycle()
    assert not engine.execute


def test_a_refused_mt5_call_is_one_warning_line_and_a_real_bug_keeps_its_traceback(caplog):
    fake = FakeMT5(now=NOW)
    config = BotConfig(symbols=(SYMBOL,), timeframe="M15", schedule=SCHEDULE)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    runner = LiveRunner(config, broker, Engine(config, broker, execute=False, live=True), [SYMBOL])

    def refused(symbol):
        raise MT5Error("no H1 bars for EURUSDm: (-1, 'Terminal: Call failed')")

    runner._process = refused
    with caplog.at_level("INFO", logger="tradebot.live"):
        runner.cycle()
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert (
        record.levelname == "WARNING"
        and record.exc_info is None
        and "skipped this cycle" in record.getMessage()
    )

    def bug(symbol):
        raise KeyError("atr")

    runner._process = bug
    caplog.clear()
    with caplog.at_level("INFO", logger="tradebot.live"):
        runner.cycle()
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.levelname == "ERROR" and record.exc_info is not None


def test_a_closed_market_is_logged_once_per_stretch_not_every_poll(caplog):
    fake = FakeMT5(now=NOW)
    config = BotConfig(symbols=(SYMBOL,), timeframe="M15", schedule=SCHEDULE)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    engine = Engine(config, broker, execute=False, live=True)
    runner = LiveRunner(config, broker, engine, [SYMBOL])
    at_close = FakeMT5(now=NOW)  # the last bars and quote before the market closed
    live_rates, live_tick = fake.copy_rates_from_pos, fake.symbol_info_tick

    def close_market(hours: float) -> None:
        fake.copy_rates_from_pos, fake.symbol_info_tick = (
            at_close.copy_rates_from_pos,
            at_close.symbol_info_tick,
        )
        fake.utc_now = NOW + timedelta(hours=hours)

    with caplog.at_level("INFO"):
        for _ in range(3):
            runner.cycle()
        assert "market closed?" not in caplog.text
        for hours in (6, 7, 8, 9, 10):  # polled all night
            close_market(hours)
            runner.cycle()
        assert caplog.text.count("market closed?") == 1
        assert caplog.text.count("quote is stale") == 1
        caplog.clear()
        fake.copy_rates_from_pos, fake.symbol_info_tick = live_rates, live_tick  # open again
        fake.utc_now = NOW + timedelta(hours=11)
        runner.cycle()
        close_market(20)  # and the next night is reported again
        runner.cycle()
        runner.cycle()
    assert caplog.text.count("market closed?") == 1
    assert caplog.text.count("quote is stale") == 1


def test_the_dashboard_and_the_loop_never_share_a_log_file():
    assert cli.LOG_FILES["run"] == "tradebot.log"
    assert cli.LOG_FILES["ui"] != cli.LOG_FILES["run"]
