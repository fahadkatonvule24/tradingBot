"""One process at a time manages trades placed by hand and sends reversal warnings (tradebot.duty):
the dashboard while it runs, the trading loop while it does not, so a restart leaves no gap."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import FakeMT5
from test_alerts import Inbox
from test_protect import NOW, SYMBOL, monitor_for
from test_protect import fake as fake

from tradebot.book import Book
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, ManagementParams, MT5Login, ScheduleParams
from tradebot.duty import Duty, FileLock
from tradebot.engine import Engine, Event
from tradebot.live import LiveRunner


def sltp(fake: FakeMT5) -> list[dict]:
    return [r for r in fake.sent if r["action"] == fake.TRADE_ACTION_SLTP]


def test_a_lock_has_one_holder_at_a_time(tmp_path):
    a, b = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock")
    assert a.acquire() and a.acquire()  # holding it already counts
    assert not b.acquire() and not b.free()
    a.release()
    assert b.free() and b.acquire()
    b.release()


def test_the_dashboard_takes_over_from_the_loop_and_hands_back_when_it_closes(tmp_path):
    loop = Duty(tmp_path, dashboard=False)
    assert loop.held()  # no dashboard: the loop manages
    dashboard = Duty(tmp_path, dashboard=True)
    assert not dashboard.held()  # the loop still has it until its next cycle
    assert dashboard.holder_text() == "the trading loop (tradebot run)"
    assert not loop.held()  # it sees the dashboard and lets go
    assert dashboard.held() and dashboard.holder_text() == "this dashboard"
    assert not loop.held()
    dashboard.close()  # closed, crashed or killed: the operating system frees its locks
    assert loop.held()
    loop.close()


def test_a_second_dashboard_stays_quiet(tmp_path):
    first, second = Duty(tmp_path, dashboard=True), Duty(tmp_path, dashboard=True)
    assert first.held() and first.primary
    assert not second.held() and not second.primary
    assert second.holder_text() == "the other dashboard that is already running"
    first.close()
    second.close()


def test_a_dashboard_without_the_duty_moves_nothing_until_the_loop_lets_go(fake, tmp_path):
    loop = Duty(tmp_path, dashboard=False)
    assert loop.held()
    inbox = Inbox()
    monitor = monitor_for(fake, True, inbox)
    monitor.duty = Duty(tmp_path, dashboard=True)
    monitor.cycle()
    assert not sltp(fake)
    state = monitor.snapshot()["breakeven"]
    assert state["on"] is False and state["elsewhere"] is True
    assert state["reason"] == "managed by the trading loop (tradebot run)"
    assert not loop.held()
    monitor.cycle()
    assert len(sltp(fake)) == 1 and monitor.snapshot()["breakeven"]["on"] is True
    monitor.duty.close()
    loop.close()


def test_a_second_dashboard_sends_nothing_to_the_phone(fake, tmp_path):
    first = Duty(tmp_path, dashboard=True)
    first.held()
    inbox = Inbox()
    monitor = monitor_for(fake, True, inbox)
    monitor.duty = Duty(tmp_path, dashboard=True)
    monitor._send("RUNNING · tradebot\ntest", "status")
    assert inbox.sent == []
    first.close()
    monitor.duty.close()


def loop_for(
    fake: FakeMT5, tmp_path, manage: bool = True, duty: Duty | None = None
) -> tuple[LiveRunner, Inbox]:
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    config = BotConfig(symbols=(SYMBOL,), timeframe="M15", management=ManagementParams(auto_breakeven=True),
                       schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""))  # fmt: skip
    inbox = Inbox()
    engine = Engine(config, broker, execute=manage, live=True)
    book = Book(tmp_path / "journal.sqlite")
    return LiveRunner(config, broker, engine, [], notifier=inbox, duty=duty, manage=manage, book=book), inbox


def test_the_trading_loop_protects_hand_trades_while_no_dashboard_runs(fake, tmp_path):
    runner, inbox = loop_for(fake, tmp_path, duty=Duty(tmp_path, dashboard=False))
    runner.cycle()
    (request,) = sltp(fake)
    assert (request["position"], request["sl"]) == (41, pytest.approx(1.0955))
    assert any(m.startswith("BREAKEVEN · EURUSDm") for m in inbox.sent)


def test_the_trading_loop_leaves_it_to_a_running_dashboard_and_to_live_trading(fake, tmp_path):
    dashboard = Duty(tmp_path, dashboard=True)
    assert dashboard.held()
    runner, _ = loop_for(fake, tmp_path, duty=Duty(tmp_path, dashboard=False))
    runner.cycle()
    assert not sltp(fake)
    dashboard.close()
    runner, _ = loop_for(fake, tmp_path, manage=False)  # a dry run sends nothing to the broker
    runner.cycle()
    assert not sltp(fake)


def test_the_loop_sends_signals_only_alerts_while_no_dashboard_runs(fake, tmp_path):
    """A signals-only symbol's signal reaches the phone from the loop only when no dashboard
    runs: a running dashboard sends its own alert for it, so the phone gets one, not two."""
    signal = Event(NOW, "signal", "ANTm", "SELL signal · signals only: the bot does not trade ANTm")
    runner, inbox = loop_for(fake, tmp_path, duty=Duty(tmp_path, dashboard=False))
    runner.engine.events.append(signal)
    runner.cycle()
    assert "ANTm: SELL signal · signals only: the bot does not trade ANTm" in inbox.sent
    assert (
        inbox.kinds[inbox.sent.index("ANTm: SELL signal · signals only: the bot does not trade ANTm")]
        == "signal"
    )
    runner.duty.close()
    dashboard = Duty(tmp_path, dashboard=True)
    assert dashboard.held()
    runner, inbox = loop_for(fake, tmp_path, duty=Duty(tmp_path, dashboard=False))
    runner.engine.events.append(signal)
    runner.cycle()
    assert not any("ANTm" in m for m in inbox.sent)
    dashboard.close()


def test_the_loop_takes_a_phone_trades_1r_from_the_first_stop_the_dashboard_saw(fake, tmp_path):
    """The phone sent the stop after the trade opened, and breakeven has moved it since: 1R is
    still the first stop, kept in the journal file, not the stop the trade has now."""
    fake.orders = [SimpleNamespace(position_id=41, time_setup=0, volume_initial=0.02, sl=0.0)]
    fake.positions[0].sl = 1.0955  # already at breakeven
    book = Book(tmp_path / "journal.sqlite")
    book.remember_stop(41, 1.0900, account=(0, "Fake-Demo"))
    book.close()
    runner, _ = loop_for(fake, tmp_path)
    (held,) = runner._positions()
    assert held.initial_sl == pytest.approx(1.0900) and held.initial_risk == pytest.approx(0.0050)


def test_the_reversal_exit_acts_on_the_terminals_trades_with_your_own_account_selected(fake, tmp_path):
    """Regression: with one of your own accounts selected the page lists that account's trades, and
    the reversal exit only ever saw those, never the trades on the terminal."""
    fake.positions[0].time = fake.server_epoch(NOW - timedelta(hours=1))  # open before the last bar closed
    book = Book(tmp_path / "journal.sqlite")
    book.save_account("Exness", 1_000.0, NOW)
    book.use("Exness")
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    config = BotConfig(symbols=(SYMBOL,), timeframe="M15", management=ManagementParams(auto_reverse_exit=True),
                       schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""))  # fmt: skip
    from tradebot.ui.monitor import Monitor

    monitor = Monitor(config, broker, [SYMBOL], notifier=Inbox(), book=book)
    monitor.cycle()  # loads the bars
    monitor._confirmed_reversal = lambda p: (
        ("warn", "Reversal warning on this buy") if p.ticket == 41 else None
    )
    monitor._keep_breakeven()
    (closing,) = [r for r in fake.sent if r["action"] == fake.TRADE_ACTION_DEAL]
    assert closing["position"] == 41 and closing["comment"] == "tradebot reversal"
    book.close()


def test_the_bots_own_trades_are_protected_by_the_dashboard_only_while_no_live_loop_runs(fake, tmp_path):
    """Get in and Duplicate orders carry the bot's number: they are the live trading loop's to
    manage, but with no loop running they must not be left without breakeven."""
    fake.positions[0].magic = BotConfig().magic
    monitor = monitor_for(fake, True, Inbox())
    monitor.duty = Duty(tmp_path, dashboard=True)
    loop = Duty(tmp_path, dashboard=False, trading=True)
    assert monitor.duty.trading_loop_running() and loop.trading_loop_running()
    monitor.cycle()
    assert not sltp(fake)  # the loop's engine has it
    loop.close()
    assert not monitor.duty.trading_loop_running()
    monitor.cycle()
    (request,) = sltp(fake)
    assert request["position"] == 41
    monitor.duty.close()
