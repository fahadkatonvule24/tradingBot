"""Regressions for the bug audit of 19 November 2026 (docs/bug-audit-2026-11-19.md), one or more
per finding, each the adverse case the audit reproduced."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL, features, live_cfg, primed
from test_advisor_ui import broker as broker
from test_advisor_ui import cfg as cfg
from test_advisor_ui import fake as fake
from test_alerts import Inbox
from test_get_in import bot_trade

from tradebot import notify
from tradebot.backtest import run_backtest
from tradebot.book import Book
from tradebot.broker.base import Position
from tradebot.broker.mt5 import MT5Broker
from tradebot.broker.sim import SimBroker
from tradebot.calculator import calculate
from tradebot.config import (
    BacktestParams,
    BotConfig,
    ConfigError,
    ManagementParams,
    MT5Login,
    RiskParams,
    ScheduleParams,
    StrategyParams,
    config_from_dict,
)
from tradebot.data import preset_spec, synthetic_bars
from tradebot.duty import Duty
from tradebot.engine import Engine
from tradebot.live import LiveRunner, trading_gate
from tradebot.notify import Notifier, TelegramError
from tradebot.plan import trading_plan
from tradebot.protect import Bar, BreakevenKeeper
from tradebot.strategy import compute_signals
from tradebot.ui.monitor import Monitor, get_in_rule

SIGNAL = {"signal": 1, "raw_signal": 1}
SCHEDULE = ScheduleParams(session="all", trade_weekends=True, news_file="")


# --- F01: a switch to a real account stops the trading loop's orders ------------------------------


def test_f01_a_switch_to_a_real_account_stops_orders_until_the_gate_passes_again(tmp_path):
    fake = FakeMT5(now=NOW)
    config = BotConfig(
        symbols=(SYMBOL,), timeframe="M15", dry_run=False, live_trading_confirm=True, schedule=SCHEDULE
    )
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)

    def gate():
        return trading_gate(config, broker.account(), True, broker.terminal_trading_allowed())

    assert gate().execute
    engine = Engine(config, broker, execute=True, live=True)
    inbox = Inbox()
    duty = Duty(tmp_path, dashboard=False, trading=True)
    runner = LiveRunner(config, broker, engine, [SYMBOL], notifier=inbox, duty=duty, manage=True, gate=gate)
    seen = []
    runner._process = lambda symbol: seen.append(engine.execute)  # what an order would have been
    try:
        runner.cycle()
        fake.trade_mode = 2  # the terminal is now logged in to a real-money account
        runner.cycle()
        assert seen == [True, False] and not runner.manage
        assert not duty.trading_loop_running()  # the dashboard manages the bot's trades meanwhile
        assert any(m.startswith("TRADING · Orders stopped") and "REAL-money" in m for m in inbox.sent)
        fake.trade_mode = 0  # back on the demo account the loop was started on
        runner.cycle()
        assert seen[-1] is True and runner.manage and duty.trading_loop_running()
    finally:
        duty.close()


def test_f01_a_dry_run_is_never_turned_into_live_trading():
    fake = FakeMT5(now=NOW)
    config = BotConfig(
        symbols=(SYMBOL,), timeframe="M15", dry_run=False, live_trading_confirm=True, schedule=SCHEDULE
    )
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    engine = Engine(config, broker, execute=False, live=True)  # started without --live
    runner = LiveRunner(
        config, broker, engine, [SYMBOL], gate=lambda: trading_gate(config, broker.account(), True, True)
    )
    runner._process = lambda symbol: None
    runner.cycle()
    assert not engine.execute


def test_f01_another_account_reads_its_own_loss_history():
    fake = FakeMT5(now=NOW)
    login = {"value": 1}
    info = fake.account_info
    fake.account_info = lambda: SimpleNamespace(**vars(info()), login=login["value"])
    config = BotConfig(symbols=(SYMBOL,), timeframe="M15", schedule=SCHEDULE)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    broker._origin[41] = (0.1, 1.09)
    engine = Engine(config, broker, execute=False, live=True)
    runner = LiveRunner(config, broker, engine, [SYMBOL])
    runner._process = lambda symbol: None
    runner.cycle()
    engine._older_deals = (NOW, 30, ["the old account's deals"])  # type: ignore[list-item]
    login["value"] = 2
    runner.cycle()
    assert engine._older_deals is None and broker._origin == {}


# --- F02: an order that timed out is never sent afterwards ---------------------------------------


def run_worker(monitor: Monitor) -> threading.Thread:
    worker = threading.Thread(target=monitor.run, daemon=True)
    worker.start()
    return worker


def test_f02_an_order_that_timed_out_in_the_queue_is_cancelled(cfg, broker, fake):
    monitor = primed(Monitor(live_cfg(cfg), broker, [SYMBOL], live_flag=True), features(SIGNAL))
    result = monitor.submit(SYMBOL, 1, True, timeout=0.001)  # the worker is not running yet
    assert not result["ok"] and "the order was not sent" in result["message"] and not result.get("pending")
    worker = run_worker(monitor)
    try:
        assert monitor.call(lambda: "after it", timeout=30) == "after it"  # the queue has been served
        assert fake.sent == []
    finally:
        monitor.stop()
        worker.join(10)


def test_f02_a_repeated_request_never_places_a_second_order(cfg, broker, fake):
    monitor = primed(Monitor(live_cfg(cfg), broker, [SYMBOL], live_flag=True), features(SIGNAL))
    worker = run_worker(monitor)
    try:
        first = monitor.submit(SYMBOL, 1, True, timeout=30, request_id="abc")
        again = monitor.submit(SYMBOL, 1, True, timeout=30, request_id="abc")
        assert first["ok"], first
        assert again["repeat"] and again["ticket"] == first["ticket"]
        assert len([r for r in fake.sent if r["action"] == fake.TRADE_ACTION_DEAL]) == 1
    finally:
        monitor.stop()
        worker.join(10)


def test_f02_an_order_already_being_sent_is_reported_as_pending(cfg, broker):
    # A long interval, and the first refresh over before the order: the worker then takes the
    # order at once. Without that, a refresh slower than the 0.5 s wait (a busy machine) left the
    # order queued, so it was cancelled instead of being sent (audit of 21 Nov 2026).
    monitor = Monitor(live_cfg(cfg), broker, [SYMBOL], live_flag=True, interval=60.0)
    release = threading.Event()
    monitor._safe_execute = lambda order: release.wait(10) and {"ok": True}  # slow broker
    worker = run_worker(monitor)
    try:
        assert monitor.call(lambda: "ready", timeout=30) == "ready"  # the first refresh is done
        result = monitor.submit(SYMBOL, 1, True, timeout=0.5)
        assert not result["ok"] and result["pending"] and "may yet be sent" in result["message"]
    finally:
        release.set()
        monitor.stop()
        worker.join(10)


# --- F03: a bar from before the entry decides nothing about the trade ----------------------------


def test_f03_half_off_needs_a_bar_after_the_entry_and_the_live_price_in_profit():
    spec = preset_spec("EURUSD")
    calls = []
    quote = {"bid": 1.1000}
    broker = SimpleNamespace(
        symbol=lambda name: spec,
        tick=lambda name: SimpleNamespace(bid=quote["bid"], ask=quote["bid"] + 0.0001),
        modify=lambda p, sl, tp, magic: calls.append(("modify", sl)) or SimpleNamespace(ok=True, message=""),
        close=lambda p, volume, magic, comment: (
            calls.append(("close", volume)) or SimpleNamespace(ok=True, message="")
        ),
    )
    config = BotConfig(management=ManagementParams(auto_partial=True, auto_breakeven=False))
    opened = datetime(2026, 3, 4, 12, 7)
    trade = Position(ticket=7, symbol="EURUSD", side=1, volume=0.10, price_open=1.1002, sl=1.0992, tp=0.0,
                     time_open=opened, initial_volume=0.10, initial_sl=1.0992, magic=0)  # fmt: skip
    before = {"EURUSD": Bar(1.1022, 0.0010, datetime(2026, 3, 4, 12, 0))}  # +2R, but before the entry
    assert BreakevenKeeper().run(broker, [trade], config, before) == [] and calls == []
    after = {"EURUSD": Bar(1.1022, 0.0010, datetime(2026, 3, 4, 12, 15))}
    assert BreakevenKeeper().run(broker, [trade], config, after) == []  # the live price is at -0.2R
    quote["bid"] = 1.1022
    ((close, _),) = BreakevenKeeper().run(broker, [trade], config, after)
    assert calls == [("close", 0.05)] and close.profit_r == pytest.approx(2.0)


# --- F04: a netting reversal's loss counts ---------------------------------------------------------


def deal(ticket, entry, side, volume, profit, minutes, position=900):
    fake = FakeMT5(now=NOW)
    return SimpleNamespace(ticket=ticket, position_id=position, symbol=SYMBOL, time=fake.server_epoch(NOW - timedelta(minutes=minutes)),
                           type=0 if side > 0 else 1, entry=entry, magic=0, volume=volume, price=1.1, profit=profit,
                           commission=-1.0, swap=0.0, fee=0.0, comment="")  # fmt: skip


def test_f04_a_reversal_deal_is_a_close_with_its_loss_and_an_opening():
    fake = FakeMT5(now=NOW)
    fake.deals = [deal(1, 0, 1, 0.1, 0.0, 30), deal(2, 2, -1, 0.3, -1000.0, 20), deal(3, 1, 1, 0.2, 50.0, 10)]
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    got = [(d.ticket, d.entry, d.volume, d.profit) for d in broker.deals(NOW - timedelta(hours=1), None)]
    assert got == [
        (1, "in", 0.1, -1.0),
        (2, "out", 0.1, -1001.0),  # closed the 0.1 buy: the realised loss
        (2, "in", pytest.approx(0.2), 0.0),  # and opened a 0.2 sell
        (3, "out", 0.2, 49.0),
    ]
    assert sum(d.profit for d in broker.deals(NOW - timedelta(hours=1), None)) == pytest.approx(-953.0)


# --- F05: a stale quote at start-up does not fix the clock for good ----------------------------------


class Clocked(FakeMT5):
    """A UTC+2 server whose only quote is ``age`` old until ``fresh`` is set."""

    def __init__(self, age: timedelta) -> None:
        super().__init__(server_offset_hours=2.0, now=NOW)
        self.age, self.fresh = age, False

    def symbol_info_tick(self, name):
        quoted = self.utc_now if self.fresh else self.utc_now - self.age
        return SimpleNamespace(time=self.server_epoch(quoted), bid=1.1, ask=1.1001)


def test_f05_a_twelve_hour_old_quote_is_corrected_when_fresh_quotes_arrive():
    fake = Clocked(timedelta(hours=12))
    broker = MT5Broker(MT5Login(), module=fake, clock=lambda: fake.utc_now)
    assert broker.offset == timedelta(hours=-10)  # indistinguishable from a UTC-10 server so far
    fake.fresh = True
    tick = broker.tick("EURUSDm")
    assert tick.problem(NOW) is None and tick.time == NOW
    assert broker.offset == timedelta(hours=2)


def test_f05_a_quote_that_is_not_live_is_a_guess_measured_again():
    fake = Clocked(timedelta(hours=12, minutes=7))
    broker = MT5Broker(MT5Login(), module=fake, clock=lambda: fake.utc_now)
    ticking = {"now": 0.0}
    broker._monotonic = lambda: ticking["now"]
    broker.server_clock  # noqa: B018 - the first measurement
    assert not broker._clock_settled
    fake.fresh = True
    ticking["now"] = 61.0
    assert broker.offset == timedelta(hours=2) and broker._clock_settled


def test_f05_a_configured_offset_is_never_measured():
    fake = Clocked(timedelta(hours=12))
    fake.fresh = True
    broker = MT5Broker(MT5Login(), offset_hours=-10.0, module=fake, clock=lambda: fake.utc_now)
    assert broker.tick("EURUSDm").time == NOW + timedelta(hours=12)  # as configured, however wrong


# --- F06: a partial fill is a fill -------------------------------------------------------------------


def test_f06_a_partial_fill_is_reported_with_the_volume_filled():
    fake = FakeMT5(now=NOW)
    fake.order_send = lambda request: SimpleNamespace(retcode=10010, order=777, price=1.1001, comment="partial",
                                                      volume=0.05)  # fmt: skip
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    from tradebot.broker.base import OrderRequest

    result = broker.open(OrderRequest(SYMBOL, 1, 0.10, 0.0015, 0.003), magic=1)
    assert result.ok and result.partial and result.volume == 0.05 and result.ticket == 777
    assert result.message == "partially filled: 0.05 of 0.1 lots"


# --- F07: every symbol's bar is processed before any decision at that moment -------------------------


def frame(start: str, closes: list[float], lows: list[float], signals: list[int]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq="15min"), "open": closes, "high": [c + 0.0002 for c in closes],
        "low": lows, "close": closes, "spread": [0] * n, "atr": [0.0010] * n, "signal": signals, "raw_signal": [0] * n,
        "rsi": [50.0] * n, "adx": [30.0] * n, "trend": [1] * n, "block": [""] * n,
    })  # fmt: skip


@pytest.mark.parametrize("other", ["A", "Z"])
def test_f07_a_stop_at_the_same_close_frees_the_slot_whatever_the_symbols_are_called(other):
    config = BotConfig(symbols=("B", other), timeframe="M15", schedule=SCHEDULE,
                       risk=RiskParams(max_total_positions=1, max_positions_per_symbol=1),
                       backtest=BacktestParams(min_spread_points=0, slippage_points=0, commission_per_lot=0))  # fmt: skip
    b = frame("2026-03-04 10:00", [1.1000, 1.0990], [1.0998, 1.0980], [1, 0])  # in at 10:15, stopped by 10:30
    a = frame("2026-03-04 10:15", [1.2000, 1.2001], [1.1998, 1.1999], [1, 0])  # signals at the 10:30 close
    specs = {name: replace(preset_spec("EURUSD"), name=name) for name in ("B", other)}
    features = {"B": b, other: a}
    result = run_backtest(config, features, specs, features=features)
    assert sorted(t.symbol for t in result.trades) == sorted(["B", other])


# --- F08: historical sizing and the simulated result use one conversion ----------------------------


def test_f08_a_usd_based_pair_risks_what_it_loses_at_the_stop():
    spec = preset_spec("USDJPY")
    params = BacktestParams(min_spread_points=0, slippage_points=0, commission_per_lot=0)
    sim = SimBroker({"USDJPY": spec}, {"USDJPY": params}, 10_000.0)
    config = BotConfig(symbols=("USDJPY",), risk=RiskParams(risk_percent=0.25, sl_atr=1.5, tp_atr=3.0))
    first = pd.Series({"time": pd.Timestamp("2026-03-04 10:00"), "open": 100.0, "high": 100.0, "low": 100.0,
                       "close": 100.0, "spread": 0, "atr": 1.0, "signal": 1, "raw_signal": 0})  # fmt: skip
    sim.start_bar("USDJPY", first, datetime(2026, 3, 4, 10, 15))
    engine = Engine(config, sim, execute=True, live=False)
    plan = engine.plan_entry("USDJPY", spec, config.for_symbol("USDJPY"), first, 0.0, 1)
    assert plan.risk_amount <= 25.0 + 1e-9  # the budget: 0.25% of 10,000
    engine._enter("USDJPY", spec, config.for_symbol("USDJPY"), first, 0.0, datetime(2026, 3, 4, 10, 15))
    stopped = first.copy()
    stopped["time"], stopped["low"], stopped["close"] = pd.Timestamp("2026-03-04 10:15"), 98.0, 98.0
    sim.start_bar("USDJPY", stopped, datetime(2026, 3, 4, 10, 30))
    (trade,) = sim.trades
    assert trade.risk_amount == pytest.approx(plan.risk_amount)
    assert trade.pnl == pytest.approx(-trade.risk_amount) and trade.r_multiple == pytest.approx(-1.0)


# --- F09 and F10: trend timeframes -----------------------------------------------------------------


def test_f09_a_trend_filter_with_nothing_above_the_timeframe_is_refused_with_the_reason():
    config = BotConfig(symbols=("EURUSD",), timeframe="D1", strategy=StrategyParams(trend_timeframes="auto"))
    with pytest.raises(ConfigError, match="no timeframe above D1"):
        config.validate()
    replace(config, strategy=replace(config.strategy, use_trend_filter=False)).validate()  # fine without it


def test_f10_a_single_trend_timeframe_is_one_timeframe_not_two_letters():
    config = config_from_dict(
        {"symbols": ["EURUSD"], "timeframe": "H1", "strategy": {"trend_timeframes": "H4"}}
    )
    config.validate()
    strategy = config.for_symbol("EURUSD").strategy
    assert strategy.trend_ladder("H1") == ("H4",)
    features = compute_signals(synthetic_bars(600, seed=3), strategy, "H1")
    assert "trend_H4" in features.columns


# --- F11: the calculator's most lots use the margin the trade does ---------------------------------


def test_f11_the_most_lots_follow_the_terminals_margin():
    fake = FakeMT5(now=NOW)
    fake.order_calc_margin = lambda kind, symbol, volume, price: (
        volume * 10_000.0
    )  # e.g. a fixed margin per lot
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    r = calculate(broker, SYMBOL, 1, stop_pips=20, lots=0.5, margin_cap=30)
    assert r["margin"] == pytest.approx(5_000.0)
    assert r["max_lots_by_cap"] == pytest.approx(0.30) and r["max_lots_by_free_margin"] == pytest.approx(1.00)
    assert r["max_lots_method"] == "the terminal's margin"
    what_if = calculate(broker, SYMBOL, 1, stop_pips=20, lots=0.5, margin_cap=30, leverage=500)
    assert what_if["max_lots_method"] == "notional / 500"


# --- F12: other accounts' sizes follow their equity ---------------------------------------------------


def test_f12_another_accounts_size_follows_its_floating_equity(cfg, broker, fake, tmp_path):
    book = Book(tmp_path / "journal.sqlite")
    book.save_account("Other", 3_000.0, NOW)
    book.open_trade("Other", SYMBOL, 1, 1.0, 1.10000, NOW, sl=1.0900)
    cfg = replace(cfg, risk=replace(cfg.risk, max_total_risk_percent=50.0))  # room beside the open trade
    monitor = Monitor(cfg, broker, [SYMBOL], book=book)
    row, params = features(SIGNAL).iloc[-1], cfg.for_symbol(SYMBOL)
    first = monitor.desk.sizes(SYMBOL, params, row, broker.tick(SYMBOL))["buy"][0]
    fake.symbol_info_tick = lambda name: SimpleNamespace(
        time=fake.server_epoch(fake.utc_now), bid=1.0850, ask=1.0851
    )
    second = monitor.desk.sizes(SYMBOL, params, row, broker.tick(SYMBOL))["buy"][0]
    assert second["equity"] < first["equity"] - 1_000  # 150 pips against a 1-lot trade
    assert second["volume"] < first["volume"]
    book.close()


# --- F14: a failed first contact with Telegram never replays old commands ---------------------------


def test_f14_old_commands_are_skipped_even_when_telegram_was_unreachable_at_first(monkeypatch):
    monkeypatch.setattr(notify, "BACKLOG_RETRY_SECONDS", (0,))
    notifier = Notifier("token", "42", background=False)
    asked = []

    def updates(offset, wait):
        asked.append(offset)
        if len(asked) == 1:
            raise TelegramError("network down")
        return [{"update_id": 5, "message": {"chat": {"id": 42}, "text": "/equity Main 10000"}}]

    notifier._updates = updates
    assert notifier._skip_backlog() == 6 and asked == [-1, -1]  # never from 0


def test_f14_a_command_sent_before_the_listener_started_is_not_acted_on():
    notifier = Notifier("token", "42", background=False)
    handled = []
    notifier._handler = lambda text: handled.append(text) or ""
    notifier._since = 1_000_000.0
    old = {"message": {"chat": {"id": 42}, "text": "/equity Main 10000", "date": 990_000}}
    new = {"message": {"chat": {"id": 42}, "text": "/equity Main 10000", "date": 1_000_010}}
    notifier.handle(old)
    notifier.handle(new)
    assert handled == ["/equity Main 10000"]


# --- F15: the plan describes the setup the symbols actually trade ----------------------------------


def test_f15_a_plan_for_symbols_that_all_override_the_main_setup_names_theirs():
    config = BotConfig(symbols=("EURUSD",), strategy=StrategyParams(name="ema_cross"),
                       overrides={"EURUSD": {"strategy": {"name": "breakout"}}})  # fmt: skip
    entry = next(s for s in trading_plan(config) if any(line.startswith("Setup") for line in s.lines))
    setup = next(line for line in entry.lines if line.startswith("Setup"))
    assert setup.startswith("Setup (Breakout)") and "EMA 10 closes" not in setup


# --- F16: the STOP file pauses the dashboard's own orders too --------------------------------------


def test_f16_get_in_and_duplicate_respect_the_stop_file(cfg, broker, fake, tmp_path):
    stop = tmp_path / "STOP"
    config = replace(
        live_cfg(cfg), kill_switch_file=str(stop), schedule=replace(cfg.schedule, session="asia")
    )
    monitor = primed(Monitor(config, broker, [SYMBOL], live_flag=True), features(SIGNAL))
    stop.write_text("")
    advice = monitor.advice_now(SYMBOL)
    assert advice.blocker.startswith("kill switch") and get_in_rule(advice) is None
    refused = monitor.execute(SYMBOL, 1, manual=True, override=True)
    assert not refused["ok"] and "new entries are paused" in refused["message"]
    bot_trade(fake, config)
    assert "new entries are paused" in monitor.duplicate(77)["message"]
    monitor.refresh()
    (position,) = monitor.snapshot()["positions"]
    assert "paused" in position["duplicate"]["refused"]
    assert fake.sent == []
    stop.unlink()
    assert monitor.execute(SYMBOL, 1, manual=True, override=True)["ok"]
