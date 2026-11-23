"""Multi-symbol entries and failures seen while reviewing the live trading path."""

import argparse
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from test_advisor_ui import features

from tradebot import cli
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams, load_config
from tradebot.duty import Duty
from tradebot.engine import Engine
from tradebot.live import Link, LiveRunner, resolve_symbols, trading_gate
from tradebot.ui.monitor import Monitor

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = load_config(ROOT / "config/config.json", env={}).symbols


def broker_for(fake):
    return MT5Broker(MT5Login(), offset_hours=2, module=fake, clock=lambda: fake.utc_now)


def add_symbol(fake, name, **changes):
    fake.symbols[name] = SimpleNamespace(**(vars(fake.symbols["EURUSDm"]) | {"name": name} | changes))


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_configured_symbols_can_send_an_order_on_a_valid_signal(fake_mt5, symbol, monkeypatch):
    """Exercise the actual config through the live runner and a fake terminal, including suffixes."""
    config = load_config(ROOT / "config/config.json", env={})
    config = replace(config, schedule=ScheduleParams(session="all", news_file="", news_feed=""))
    name = f"{symbol}m"
    stock = bool(config.for_symbol(name).risk.market_hours)
    if stock:  # a US stock trades 09:30-16:00 New York: 15:07 UTC is 10:07 there (EST, 4 March)
        fake_mt5.utc_now = fake_mt5.utc_now.replace(hour=15)
    add_symbol(fake_mt5, name)
    broker = broker_for(fake_mt5)
    engine = Engine(config, broker, execute=True, live=True)
    frame = features({"signal": 1, "raw_signal": 1})
    # Last closed H1 bar, at realistic FX prices, independently of the setup's trigger generation.
    frame.loc[frame.index[-1], "time"] = fake_mt5.utc_now.replace(minute=0) - pd.Timedelta(hours=1)
    monkeypatch.setattr("tradebot.live.latest_features", lambda *args: frame)
    runner = LiveRunner(config, broker, engine, [broker.resolve(symbol)])
    runner.cycle()
    runner.cycle()  # the same signal must not be traded twice
    assert len(fake_mt5.sent) == 1
    assert fake_mt5.sent[0]["symbol"] == name
    assert fake_mt5.sent[0]["sl"] < fake_mt5.sent[0]["price"] < fake_mt5.sent[0]["tp"]


@pytest.mark.parametrize(
    "symbol",
    [s for s in SYMBOLS if load_config(ROOT / "config/config.json", env={}).for_symbol(s).risk.market_hours],
)
def test_configured_stocks_send_no_order_before_the_open(fake_mt5, symbol, monkeypatch):
    """12:07 UTC is 07:07 in New York: Exness quotes the stocks but refuses orders (AMZN, 23 Nov)."""
    config = load_config(ROOT / "config/config.json", env={})
    config = replace(config, schedule=ScheduleParams(session="all", news_file="", news_feed=""))
    add_symbol(fake_mt5, f"{symbol}m")
    broker = broker_for(fake_mt5)
    engine = Engine(config, broker, execute=True, live=True)
    frame = features({"signal": 1, "raw_signal": 1})
    frame.loc[frame.index[-1], "time"] = fake_mt5.utc_now.replace(hour=11, minute=0)
    monkeypatch.setattr("tradebot.live.latest_features", lambda *args: frame)
    LiveRunner(config, broker, engine, [broker.resolve(symbol)]).cycle()
    assert fake_mt5.sent == []
    assert engine.skips["market"] == 1


@pytest.mark.parametrize("mode", [0, 3])
def test_disabled_exact_symbol_does_not_hide_a_tradable_suffix(fake_mt5, mode):
    add_symbol(fake_mt5, "EURUSD", trade_mode=mode)
    assert broker_for(fake_mt5).resolve("EURUSD") == "EURUSDm"


def test_unselectable_symbol_falls_back_to_an_available_suffix(fake_mt5):
    fake_mt5.symbols["EURUSDm"].visible = False
    add_symbol(fake_mt5, "EURUSD.raw", visible=True)
    fake_mt5.symbol_select = lambda name, enable: False
    assert broker_for(fake_mt5).resolve("EURUSD") == "EURUSD.raw"


def test_unselectable_symbol_is_unavailable(fake_mt5):
    fake_mt5.symbols["EURUSDm"].visible = False
    fake_mt5.symbol_select = lambda name, enable: False
    assert broker_for(fake_mt5).resolve("EURUSD") is None


def test_exact_tradable_name_is_preferred_to_a_suffix(fake_mt5):
    add_symbol(fake_mt5, "EURUSD", trade_mode=4)
    assert broker_for(fake_mt5).resolve("EURUSD") == "EURUSD"


def test_terminal_without_a_server_connection_stays_in_recovery(fake_mt5):
    fake_mt5.terminal_info = lambda: SimpleNamespace(connected=False, trade_allowed=True)
    broker = broker_for(fake_mt5)
    assert not broker.connected()
    assert not broker.terminal_trading_allowed()
    link = Link(broker)
    assert not link.ok()  # initialize() alone succeeding does not restore the connection
    assert link.down_since is not None
    fake_mt5.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    assert link.ok()


def test_second_live_loop_cannot_start_in_the_same_folder(tmp_path):
    first = Duty(tmp_path, dashboard=False, trading=True)
    try:
        with pytest.raises(ValueError, match="another live trading loop"):
            Duty(tmp_path, dashboard=False, trading=True)
    finally:
        first.close()


def test_loop_cannot_resume_orders_while_another_loop_holds_the_lock(fake_mt5, tmp_path):
    config = BotConfig(dry_run=False, live_trading_confirm=True)
    broker = broker_for(fake_mt5)
    first = Duty(tmp_path, dashboard=False, trading=True)
    second = None
    engine = Engine(config, broker, execute=True, live=True)
    runner = LiveRunner(
        config,
        broker,
        engine,
        [],
        duty=first,
        manage=True,
        gate=lambda: trading_gate(config, broker.account(), True, broker.terminal_trading_allowed()),
    )
    try:
        fake_mt5.trade_mode = 2  # orders pause and the loop releases its lock
        runner._authorize()
        assert not engine.execute
        second = Duty(tmp_path, dashboard=False, trading=True)
        fake_mt5.trade_mode = 0
        runner._authorize()
        assert not engine.execute and not runner.manage
        second.close()
        runner._authorize()
        assert engine.execute and runner.manage
    finally:
        first.close()
        if second is not None:
            second.close()


@pytest.mark.parametrize("dashboard", [False, True])
def test_account_switch_refreshes_symbol_names_and_discards_old_prices(fake_mt5, dashboard):
    account = fake_mt5.account_info
    login = [1]
    fake_mt5.account_info = lambda: SimpleNamespace(**vars(account()), login=login[0])
    config = BotConfig(
        symbols=("EURUSD",), timeframe="M15", schedule=ScheduleParams(session="all", news_file="")
    )
    broker = broker_for(fake_mt5)
    if dashboard:
        worker = Monitor(config, broker, ["EURUSDm"])
        worker.refresh()
    else:
        worker = LiveRunner(config, broker, Engine(config, broker, execute=False, live=True), ["EURUSDm"])
        worker.cycle()
    assert "EURUSDm" in worker._features
    old = fake_mt5.symbols.pop("EURUSDm")
    fake_mt5.symbols["EURUSD"] = SimpleNamespace(**(vars(old) | {"name": "EURUSD"}))
    login[0] = 2
    if dashboard:
        worker.refresh()
        assert not any("error" in row for row in worker.snapshot()["symbols"])
    else:
        worker.cycle()
    assert worker.symbols == ["EURUSD"]
    assert "EURUSDm" not in worker._features
    assert "EURUSD" in worker._features


def test_switching_accounts_clears_the_previous_accounts_acted_bars(fake_mt5):
    broker = broker_for(fake_mt5)
    engine = Engine(BotConfig(), broker, execute=False, live=True)
    engine._acted_bar["EURUSDm"] = features().iloc[-1]["time"]
    engine.forget_account()
    assert not engine._acted_bar


def test_watchlist_skips_unavailable_symbols_and_deduplicates_aliases(fake_mt5):
    assert resolve_symbols(broker_for(fake_mt5), ["MISSING", "EURUSD", "EURUSDm"]) == ["EURUSDm"]


def test_failed_account_check_stops_both_entries_and_trade_management(fake_mt5, tmp_path):
    config = BotConfig(dry_run=False, live_trading_confirm=True)
    broker = broker_for(fake_mt5)
    duty = Duty(tmp_path, dashboard=False, trading=True)
    engine = Engine(config, broker, execute=True, live=True)
    runner = LiveRunner(config, broker, engine, ["EURUSDm"], duty=duty, manage=True)
    calls = []
    runner._process = lambda symbol: calls.append(symbol)
    runner._protect = lambda: calls.append("manage")
    fake_mt5.account_info = lambda: None
    try:
        runner.cycle()
        assert not calls and not engine.execute and not runner.manage
        assert not duty.trading_loop_running()
    finally:
        duty.close()


def test_check_reports_modes_and_keeps_checking_after_one_bad_symbol(fake_mt5, monkeypatch, capsys):
    config = BotConfig(symbols=("EURUSD", "GBPUSD"), overrides={"EURUSD": {"risk": {"auto_trade": False}}})
    add_symbol(fake_mt5, "GBPUSDm")
    broker = broker_for(fake_mt5)
    monkeypatch.setattr(cli, "_connect", lambda config: (broker, broker.account(), ["EURUSDm", "GBPUSDm"]))

    def latest(broker, config, symbol):
        if symbol == "EURUSDm":
            raise ValueError("no bars")
        return features({"signal": 1, "raw_signal": 1})

    monkeypatch.setattr("tradebot.live.latest_features", latest)
    assert cli.cmd_check(config, argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "EURUSDm" in output and "GBPUSDm" in output
    assert "enabled" in output and "BUY" in output
    assert not fake_mt5.sent


def test_failed_watchlist_startup_closes_the_terminal_connection(fake_mt5, monkeypatch):
    broker = broker_for(fake_mt5)
    closed = []
    monkeypatch.setattr(broker, "shutdown", lambda: closed.append(True))
    monkeypatch.setattr("tradebot.broker.mt5.MT5Broker", lambda *args: broker)
    with pytest.raises(ValueError, match="none of the configured symbols"):
        cli._connect(BotConfig(symbols=("MISSING",)))
    assert closed == [True]


def test_second_loop_startup_releases_resources_without_starting_notifications(
    fake_mt5, tmp_path, monkeypatch
):
    broker = broker_for(fake_mt5)
    config = BotConfig(dry_run=False, live_trading_confirm=True, journal=str(tmp_path / "journal.sqlite"))
    monkeypatch.setattr(cli, "_connect", lambda config: (broker, broker.account(), ["EURUSDm"]))
    closed, notifications = [], []
    monkeypatch.setattr(broker, "shutdown", lambda: closed.append(True))
    monkeypatch.setattr(cli, "_notifier", lambda config: notifications.append(True))
    first = Duty(tmp_path, dashboard=False, trading=True)
    try:
        with pytest.raises(ValueError, match="another live trading loop"):
            cli.cmd_run(config, argparse.Namespace(live=True, once=True))
        assert closed == [True] and not notifications and not fake_mt5.sent
    finally:
        first.close()
