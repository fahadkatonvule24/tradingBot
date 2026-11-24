"""Regressions from the whole-system audit of 21 Nov 2026."""

from __future__ import annotations

import argparse
from dataclasses import replace

import pandas as pd
import pytest
from test_advisor_ui import broker as broker
from test_advisor_ui import cfg as cfg
from test_advisor_ui import fake as fake

from tradebot import cli
from tradebot.broker.base import SymbolSpec
from tradebot.config import BotConfig, RiskParams


def test_a_symbol_takes_the_longest_matching_override():
    """Overrides match the broker's name by prefix: with "T" (AT&T) and "TSLA" both set, TSLAm
    must get TSLA's, whichever comes first in the file."""
    config = BotConfig(
        symbols=("T", "TSLA"),
        overrides={"T": {"risk": {"auto_trade": False}}, "TSLA": {"risk": {"risk_percent": 0.5}}},
    )
    assert config.for_symbol("TSLAm").risk == replace(RiskParams(), risk_percent=0.5)
    assert config.for_symbol("Tm").risk.auto_trade is False
    assert config.for_symbol("TSLA").risk.risk_percent == 0.5  # an exact name still wins


def test_a_trade_open_when_the_history_stops_leaves_at_the_last_price_seen(config, eurusd):
    """A stock's history had a three-month hole; a trade "held" through it exited at the first
    bar after it and booked +8R that nobody could have had."""
    from conftest import at
    from test_engine import flat

    from tradebot.backtest import run_backtest

    rows = [flat(0, signal=1, raw_signal=1), flat(15), flat(30, 1.1002)]
    after = pd.Timestamp(at(30)) + pd.Timedelta(days=90)
    rows.append({**flat(45, 1.1500), "time": after})  # far through the target, three months on
    rows.append({**flat(60, 1.1500), "time": after + pd.Timedelta(minutes=15)})
    bars = pd.DataFrame(rows).assign(exit_signal=0)
    result = run_backtest(config, {"EURUSD": bars}, {"EURUSD": eurusd}, features={"EURUSD": bars})
    (trade,) = result.trades
    assert trade.exit_reason == "data hole"
    assert trade.exit_price == 1.1002 and trade.exit_time < after


def test_a_weekend_is_not_a_data_hole(config, eurusd):
    from conftest import at
    from test_engine import flat

    from tradebot.backtest import run_backtest

    rows = [flat(0, signal=1, raw_signal=1), flat(15)]
    monday = pd.Timestamp(at(15)) + pd.Timedelta(days=2, hours=12)
    rows.append({**flat(30, 1.1001), "time": monday})
    bars = pd.DataFrame(rows).assign(exit_signal=0)
    result = run_backtest(config, {"EURUSD": bars}, {"EURUSD": eurusd}, features={"EURUSD": bars})
    assert [t.exit_reason for t in result.trades] == ["end of test"]


def test_swap_nights_follow_the_new_york_rollover_and_the_triple_day():
    from datetime import datetime

    from tradebot.broker.sim import rollovers

    # 21:00 UTC is 17:00 in New York in summer (EDT), 22:00 in winter (EST)
    assert rollovers(datetime(2026, 7, 6, 12), datetime(2026, 7, 6, 20), 2) == 0  # Monday, same day
    assert rollovers(datetime(2026, 7, 6, 12), datetime(2026, 7, 6, 21), 2) == 1  # through Monday's
    assert rollovers(datetime(2026, 1, 5, 12), datetime(2026, 1, 5, 21, 30), 2) == 0  # winter: 22:00
    assert rollovers(datetime(2026, 7, 8, 12), datetime(2026, 7, 9, 12), 2) == 3  # Wednesday: triple
    assert rollovers(datetime(2026, 7, 8, 12), datetime(2026, 7, 9, 12), 4) == 1  # a stock: not Wednesday
    assert rollovers(datetime(2026, 7, 10, 12), datetime(2026, 7, 13, 12), 4) == 3  # stock over a weekend
    assert rollovers(datetime(2026, 7, 10, 12), datetime(2026, 7, 13, 12), 2) == 1  # forex: Friday is one
    assert rollovers(datetime(2026, 7, 6, 21), datetime(2026, 7, 7, 12), 2) == 0  # opened at the rollover


def test_a_trade_held_overnight_pays_the_brokers_swap(config, eurusd):
    from conftest import at
    from test_engine import flat

    from tradebot.backtest import run_backtest

    costly = replace(config, backtest=replace(config.backtest, commission_per_lot=0.0, swap_long=-10.0))
    rows = [flat(0, signal=1, raw_signal=1), flat(15)]
    later = pd.Timestamp(at(15)) + pd.Timedelta(days=1)  # through one rollover (not a Wednesday)
    rows.append({**flat(30), "time": later})
    bars = pd.DataFrame(rows).assign(exit_signal=0)
    free = run_backtest(replace(costly, backtest=replace(costly.backtest, swap_long=0.0)), {"EURUSD": bars},
                        {"EURUSD": eurusd}, features={"EURUSD": bars})  # fmt: skip
    paid = run_backtest(costly, {"EURUSD": bars}, {"EURUSD": eurusd}, features={"EURUSD": bars})
    (with_swap,), (without,) = paid.trades, free.trades
    nights = 3 if pd.Timestamp(at(15)).day_of_week == 2 else 1
    assert with_swap.pnl == pytest.approx(without.pnl - 10.0 * with_swap.volume * nights)


def test_the_triple_swap_day_is_validated():
    from tradebot.config import ConfigError

    bad = BotConfig(backtest=replace(BotConfig().backtest, swap_triple_day=7))
    with pytest.raises(ConfigError, match="swap_triple_day"):
        bad.validate()


def test_review_finds_the_first_stop_saved_for_the_logged_in_account(tmp_path):
    """First stops are saved per (login, server); `tradebot review` still looked them up by ticket
    alone, so a phone trade that opened without a stop lost its 1R again (audit of 23 Nov 2026)."""
    from types import SimpleNamespace

    from tradebot.book import Book
    from tradebot.cli import _SeenStops

    book = Book(tmp_path / "journal.sqlite")
    book.remember_stop(41, 1.09, account=(101, "Broker-A"))
    book.remember_stop(41, 1.05, account=(202, "Broker-A"))  # the same ticket on another account
    terminal = SimpleNamespace(opening=lambda ticket: (0.1, None), account_id=lambda: (101, "Broker-A"))
    assert _SeenStops(terminal, book).opening(41) == (0.1, 1.09)
    terminal.account_id = lambda: (303, "Broker-B")
    assert _SeenStops(terminal, book).opening(41) == (0.1, None)  # never another account's stop
    with_stop = SimpleNamespace(opening=lambda ticket: (0.1, 1.08), account_id=lambda: (101, "Broker-A"))
    assert _SeenStops(with_stop, book).opening(41) == (0.1, 1.08)  # the terminal's own stop first
    book.close()


def test_a_stop_saved_before_accounts_were_kept_goes_to_the_account_that_owns_the_ticket(tmp_path):
    """Ten first stops in the real journal predate per-account stops; four phone trades need one
    for their 1R. The account whose own history holds the ticket claims it; no other can after."""
    from types import SimpleNamespace

    from tradebot.book import Book
    from tradebot.cli import _SeenStops

    book = Book(tmp_path / "journal.sqlite")
    book.remember_stop(41, 1.09)  # the old way: no account
    terminal = SimpleNamespace(opening=lambda ticket: (0.1, None), account_id=lambda: (101, "Broker-A"))
    assert _SeenStops(terminal, book).opening(41) == (0.1, 1.09)
    assert book.seen_stop(41) is None  # moved, not copied
    assert book.seen_stop(41, account=(101, "Broker-A")) == 1.09
    assert book.first_stop(41, (202, "Broker-A")) is None  # another account never gets it
    assert book.first_stop(42, (101, "Broker-A")) is None
    book.close()


def test_a_trade_open_across_the_upgrade_keeps_its_first_stop_not_its_moved_one(fake_mt5, tmp_path):
    from types import SimpleNamespace

    from tradebot.book import Book, BookBroker
    from tradebot.broker.mt5 import MT5Broker
    from tradebot.config import MT5Login

    book = Book(tmp_path / "journal.sqlite")
    book.remember_stop(41, 1.0900)  # saved before the upgrade, without an account
    fake_mt5.positions = [SimpleNamespace(ticket=41, magic=0, symbol="EURUSDm", type=0, volume=0.1,
                                          price_open=1.1, sl=1.1000, tp=0.0,  # at breakeven now
                                          time=fake_mt5.server_epoch(pd.Timestamp("2026-03-04 10:00").to_pydatetime()),
                                          profit=0.0, comment="")]  # fmt: skip
    fake_mt5.orders = [
        SimpleNamespace(position_id=41, time_setup=0, volume_initial=0.1, sl=0.0)
    ]  # phone: no stop
    wrapped = BookBroker(MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5), book, 1)
    (position,) = wrapped.terminal_positions(None)
    assert position.initial_sl == 1.09  # 1R from the first stop, not from breakeven
    book.close()


def test_us_stocks_take_orders_only_in_new_york_trading_hours():
    """Exness quotes US stocks from 06:00 New York but refuses orders before 09:30: AMZN's pre-market
    BUY was rejected live on 23 Nov 2026 ("session closed")."""
    from datetime import datetime

    from tradebot.config import market_closed

    def is_open(*utc):
        return market_closed("us_stocks", datetime(*utc)) is None

    assert not is_open(2026, 11, 23, 12, 0)  # 07:00 New York (EST): pre-market, the rejected order
    assert not is_open(2026, 11, 23, 14, 29) and is_open(2026, 11, 23, 14, 30)  # opens 09:30
    assert is_open(2026, 11, 23, 20, 59) and not is_open(2026, 11, 23, 21, 0)  # closes 16:00
    assert is_open(2026, 7, 6, 13, 30) and not is_open(2026, 7, 6, 20, 0)  # summer (EDT): an hour earlier
    assert not is_open(2026, 11, 21, 15, 0)  # Saturday
    assert market_closed("", datetime(2026, 11, 21, 3, 0)) is None  # forex: whenever quoted
    assert not is_open(2026, 11, 26, 16, 0)  # Thanksgiving, 11:00 New York
    assert "holiday" in market_closed("us_stocks", datetime(2026, 11, 26, 16, 0))
    assert is_open(2026, 11, 27, 17, 59) and not is_open(2026, 11, 27, 18, 0)  # half day: 13:00 close
    assert is_open(2026, 11, 30, 18, 0)  # the Monday after: a full day again


def test_a_stock_signal_before_the_open_is_skipped_and_one_after_it_is_taken(config, eurusd):
    from test_engine import flat, setup, step

    stock = replace(config, overrides={"EURUSD": {"risk": {"market_hours": "us_stocks"}}})
    config, sim, engine = setup(stock, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))  # closes 10:15 UTC: 05:15 New York
    assert not sim.positions(config.magic) and engine.events[-1].detail.startswith(
        "buy signal skipped: market:"
    )
    step(engine, sim, config, eurusd, flat(300, signal=1, raw_signal=1))  # closes 15:15 UTC: 10:15 New York
    assert len(sim.positions(config.magic)) == 1


def test_a_stocks_stop_is_left_alone_until_the_exchange_opens():
    """Breakeven due on a stock in the pre-market: the broker refuses the change, and the phone got
    "STOP NOT MOVED". Held until 09:30 New York, then sent."""
    from datetime import datetime
    from types import SimpleNamespace

    from test_protect import SPEC, position

    from tradebot.config import ManagementParams
    from tradebot.protect import BreakevenKeeper

    sent = []
    broker = SimpleNamespace(
        symbol=lambda name: SPEC, tick=lambda name: SimpleNamespace(bid=1.1000, ask=1.1001),
        modify=lambda p, sl, tp, magic: sent.append(sl) or SimpleNamespace(ok=True, message=""),
    )  # fmt: skip
    config = BotConfig(management=ManagementParams(auto_breakeven=True),
                       overrides={"EURUSD": {"risk": {"market_hours": "us_stocks"}}})  # fmt: skip
    keeper = BreakevenKeeper()
    keeper.run(broker, [position()], config, now=datetime(2026, 11, 23, 12, 0))  # 07:00 New York
    assert sent == []
    keeper.run(broker, [position()], config, now=datetime(2026, 11, 23, 15, 0))  # 10:00 New York
    assert len(sent) == 1


def test_the_bots_own_stock_trade_is_not_managed_before_the_open(config, eurusd):
    from test_engine import flat, setup, step

    stock = replace(config, overrides={"EURUSD": {"risk": {"market_hours": "us_stocks"}}})
    config, sim, engine = setup(stock, eurusd)
    step(engine, sim, config, eurusd, flat(300, signal=1, raw_signal=1))  # 10:15 New York: opened
    (opened,) = sim.positions(config.magic)
    overnight = pd.Timestamp(at_minutes(300)) + pd.Timedelta(hours=16)  # 07:15 New York next day
    step(
        engine, sim, config, eurusd, {**flat(330, 1.1020, signal=-1, raw_signal=-1), "time": overnight}
    )  # +1.3R: breakeven due
    assert sim.positions(config.magic) == [opened]  # the opposite signal waits for the open
    assert opened.sl == opened.initial_sl  # and breakeven too


def at_minutes(minutes):
    from conftest import at

    return at(minutes)


def test_get_in_never_sends_an_order_to_a_closed_exchange():
    from types import SimpleNamespace

    from tradebot.ui.monitor import get_in_rule

    shut = SimpleNamespace(
        action="SKIP", side=1, blocker="market: the exchange is closed", plan=SimpleNamespace(rejected="")
    )
    assert get_in_rule(shut) is None


def test_an_unknown_market_hours_name_is_rejected():
    from tradebot.config import ConfigError

    with pytest.raises(ConfigError, match="market_hours"):
        BotConfig(overrides={"EURUSD": {"risk": {"market_hours": "nyse"}}}).validate()


class _Broker:
    def resolve(self, name: str) -> str | None:
        return None if name == "GONE" else f"{name}m"

    def symbol(self, name: str) -> SymbolSpec:
        return SymbolSpec(name, 2, 0.01, 0.01, 1.0, 100, 0.01, 10, 0.01, 0, "USD", "USD")

    def shutdown(self) -> None:
        pass


def test_fetch_saves_each_symbols_history_under_its_own_name(tmp_path, monkeypatch):
    """A configured symbol the broker lacks used to shift every later one by a place: AMD's
    bars were written to META's file."""
    monkeypatch.setattr(cli, "_connect", lambda config: (_Broker(), None, ["AMDm", "METAm"]))

    def history(broker, symbol, timeframe, count):
        return pd.DataFrame({"time": [pd.Timestamp("2026-11-20 10:00")], "close": [float(len(symbol))],
                             "source": [symbol]})  # fmt: skip

    monkeypatch.setattr("tradebot.broker.mt5.fetch_history", history)
    config = BotConfig(symbols=("AMD", "GONE", "META"), timeframe="H1")
    args = argparse.Namespace(out=tmp_path, timeframes=["H1"], bars=10)
    assert cli.cmd_fetch(config, args) == 0
    assert pd.read_csv(tmp_path / "AMD_H1.csv")["source"].tolist() == ["AMDm"]
    assert pd.read_csv(tmp_path / "META_H1.csv")["source"].tolist() == ["METAm"]
    assert not (tmp_path / "GONE_H1.csv").exists()


def _hourly(start: str, hours: int) -> pd.DataFrame:
    times = pd.date_range(start, periods=hours, freq="h")
    price = pd.Series(range(hours), dtype=float) / 10 + 100
    return pd.DataFrame({"time": times, "open": price, "high": price + 1, "low": price - 1, "close": price,
                         "spread": 10, "tick_volume": 100})  # fmt: skip


def test_a_stock_reads_only_its_regular_session_bars():
    """30% of the stock H1 bars are pre-market quotes (half the range, 2-4x the spread)."""
    from tradebot.strategy import regular_session

    winter = _hourly("2026-11-23 10:00", 12)  # Monday 05:00-16:00 New York (EST)
    kept = regular_session(winter, "us_stocks", "H1")
    hours_ny = kept["time"].dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.hour.tolist()
    assert hours_ny == [9, 10, 11, 12, 13, 14, 15]  # the 09:00 bar holds the opening half hour
    summer = _hourly("2026-07-06 09:00", 12)  # Monday 05:00-16:00 New York (EDT)
    assert len(regular_session(summer, "us_stocks", "H1")) == 7
    saturday = _hourly("2026-11-21 14:00", 3)
    assert regular_session(saturday, "us_stocks", "H1").empty
    assert len(regular_session(winter, "", "H1")) == 12  # forex: every bar
    assert len(regular_session(winter, "us_stocks", "D1")) == 12  # daily bars: whole


def test_a_stocks_atr_ignores_the_pre_market():
    from tradebot.config import StrategyParams
    from tradebot.strategy import compute_signals

    bars = pd.concat([_hourly(f"2026-11-{d} 10:00", 12) for d in (16, 17, 18, 19, 20, 23, 24, 25)])
    bars = bars.reset_index(drop=True)
    pre = bars["time"].dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.hour < 9
    bars.loc[pre, "high"] = bars.loc[pre, "close"] + 0.01  # quiet pre-market quotes
    bars.loc[pre, "low"] = bars.loc[pre, "close"] - 0.01
    p = StrategyParams(use_trend_filter=False, atr_period=14)
    stock = compute_signals(bars, p, "H1", market_hours="us_stocks")
    every = compute_signals(bars, p, "H1")
    hour = bars["time"].dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.hour
    assert len(stock) == int(((hour >= 9) & (hour <= 15)).sum())  # pre-market and after the close dropped
    assert stock["atr"].iloc[-1] > every["atr"].iloc[-1] * 1.2  # the thin quotes no longer shrink it (+25%)


def test_the_dashboard_asks_a_closed_market_for_bars_once_a_minute(cfg, broker, monkeypatch):
    from types import SimpleNamespace

    from test_advisor_ui import SYMBOL

    from tradebot.ui import monitor as module
    from tradebot.ui.monitor import Monitor

    clock = [1000.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    friday = _hourly("2026-11-20 10:00", 11)  # the market's last bars, Friday

    def fetch(broker, config, symbol):
        calls.append(clock[0])
        return friday, {}

    monkeypatch.setattr(module, "fetch_frames", fetch)
    monitor = Monitor(cfg, broker, [SYMBOL])
    sunday = datetime_utc(2026, 11, 22, 12, 0)
    monitor._features_for(SYMBOL, sunday)
    for _ in range(30):  # every 5 s for 2.5 minutes
        clock[0] += 5
        monitor._features_for(SYMBOL, sunday)
    assert len(calls) == 3  # the first look, then once a minute: not every 5 s


def datetime_utc(*parts):
    from datetime import datetime

    return datetime(*parts)


def test_new_stock_entries_pause_around_an_earnings_report(tmp_path):
    """Reports come before the open or after the close: the gap is on the report day or the next."""
    import json
    from datetime import date, datetime

    from tradebot.earnings import EarningsCalendar

    path = tmp_path / "earnings.json"
    path.write_text(json.dumps({"_how": "notes are skipped", "AAPL": ["2027-01-28"], "A": ["2027-03-01"]}))
    calendar = EarningsCalendar(path)
    report = date(2027, 1, 28)  # a Thursday
    assert calendar.blackout("AAPLm", datetime(2027, 1, 26, 15), 1, 1) is None  # Tuesday: two days before
    assert calendar.blackout("AAPLm", datetime(2027, 1, 27, 15), 1, 1) == report  # the day before
    assert calendar.blackout("AAPLm", datetime(2027, 1, 29, 15), 1, 1) == report  # the day after
    assert calendar.blackout("AAPLm", datetime(2027, 2, 1, 15), 1, 1) is None  # Monday after: clear
    assert calendar.next_report("AAPLm", datetime(2027, 1, 2)) == report and calendar.known("AAPLm")
    assert not calendar.known("TSLAm") and calendar.blackout("TSLAm", datetime(2027, 1, 27, 15), 1, 1) is None
    assert calendar.blackout("AMDm", datetime(2027, 3, 1, 15), 1, 1) == date(2027, 3, 1)  # "A" by prefix
    friday = EarningsCalendar(path)
    path.write_text(json.dumps({"AAPL": ["2027-02-01"]}))  # a Monday report: the Friday before pauses
    assert friday.blackout("AAPLm", datetime(2027, 1, 29, 15), 1, 1) == date(2027, 2, 1)
    path.write_text("not json")
    broken = EarningsCalendar(path)
    assert (
        broken.blackout("AAPLm", datetime(2027, 1, 29, 15), 1, 1) is None
        and "could not be read" in broken.error
    )
    assert EarningsCalendar(tmp_path / "missing.json").next_report("AAPLm", datetime(2027, 1, 2)) is None


def test_the_engine_skips_a_stock_signal_in_its_earnings_pause(config, eurusd, tmp_path):
    import json

    from conftest import at
    from test_engine import flat, setup, step

    path = tmp_path / "earnings.json"
    path.write_text(json.dumps({"EURUSD": [str(pd.Timestamp(at(0)).date())]}))  # "reports" today
    paused = replace(config, schedule=replace(config.schedule, earnings_file=str(path)))
    config, sim, engine = setup(paused, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    assert not sim.positions(config.magic) and engine.events[-1].detail.startswith(
        "buy signal skipped: earnings:"
    )


def test_earnings_pause_days_are_validated():
    from tradebot.config import ConfigError, ScheduleParams

    with pytest.raises(ConfigError, match="earnings_days"):
        BotConfig(schedule=ScheduleParams(earnings_days_before=-1)).validate()


def test_the_dashboard_row_says_a_stocks_hours_shares_and_earnings(cfg, broker):
    from test_advisor_ui import SYMBOL, features, primed

    from tradebot.ui.monitor import Monitor

    stock = replace(cfg, overrides={SYMBOL: {"risk": {"market_hours": "us_stocks"}}},
                    schedule=replace(cfg.schedule, earnings_file=""))  # fmt: skip
    monitor = primed(Monitor(stock, broker, [SYMBOL]), features())
    monitor.refresh()
    row = monitor.snapshot()["symbols"][0]
    assert row["market_hours"] == "09:30 to 16:00 New York time"
    assert row["contract_size"] > 0 and row["earnings_known"] is False and row["earnings_next"] is None
    assert isinstance(row["market_open"], bool)
    forex = primed(Monitor(cfg, broker, [SYMBOL]), features())
    forex.refresh()
    assert forex.snapshot()["symbols"][0]["market_hours"] == ""


def test_a_known_earnings_date_reaches_the_page_as_text(cfg, broker, tmp_path):
    """Regression (24 Nov 2026): the next report went out as a date object, so every /api/state
    request failed (the server's json.dumps raised) once earnings.json had a date in it."""
    import json

    from test_advisor_ui import SYMBOL, features, primed

    from tradebot.ui.monitor import Monitor

    dates = tmp_path / "earnings.json"
    dates.write_text(json.dumps({SYMBOL[:6]: ["~2099-01-28"]}), encoding="utf-8")
    stock = replace(cfg, overrides={SYMBOL: {"risk": {"market_hours": "us_stocks"}}},
                    schedule=replace(cfg.schedule, earnings_file=str(dates)))  # fmt: skip
    monitor = primed(Monitor(stock, broker, [SYMBOL]), features())
    monitor.refresh()
    snapshot = monitor.snapshot()
    json.dumps(snapshot, allow_nan=False)  # what the server sends; it raised before
    row = snapshot["symbols"][0]
    assert row["earnings_next"] == "2099-01-28" and row["earnings_estimated"] is True


def test_a_signals_only_alert_states_the_real_risk_of_the_smallest_lot(config, eurusd):
    """Gold: the smallest lot risks ~3.8% while risk_percent is 1.5; the alert said "1.5% risk"."""
    from conftest import make_sim
    from test_engine import flat, step

    from tradebot.engine import Engine

    small = replace(config, overrides={"EURUSD": {"risk": {"auto_trade": False, "allow_min_lot_overrisk": True,
                                                            "risk_percent": 1.5}}})  # fmt: skip
    sim = make_sim({"EURUSD": eurusd}, balance=40.0)  # 0.01 lot at a 15-pip stop: $1.50, 3.8%
    engine = Engine(small, sim, execute=True, live=True)
    step(engine, sim, small, eurusd, flat(0, signal=1, raw_signal=1))
    signal = engine.events[-1]
    assert signal.kind == "signal" and "0.01 lots" in signal.detail
    assert "(3.8% risk, over your 1.5% a trade)" in signal.detail


def test_an_estimated_earnings_date_pauses_a_day_wider_each_side(tmp_path):
    import json
    from datetime import date, datetime

    from tradebot.earnings import EarningsCalendar

    path = tmp_path / "earnings.json"
    path.write_text(json.dumps({"AAPL": ["~2027-01-28"], "MSFT": ["2027-01-28"]}))  # Thursday
    calendar = EarningsCalendar(path)
    report = date(2027, 1, 28)
    assert calendar.is_estimated("AAPLm", report) and not calendar.is_estimated("MSFTm", report)
    tuesday, monday_after = datetime(2027, 1, 26, 15), datetime(2027, 2, 1, 15)
    assert calendar.blackout("AAPLm", tuesday, 1, 1) == report  # two trading days before
    assert calendar.blackout("AAPLm", monday_after, 1, 1) == report  # two after (over the weekend)
    assert (
        calendar.blackout("MSFTm", tuesday, 1, 1) is None
        and calendar.blackout("MSFTm", monday_after, 1, 1) is None
    )
    assert calendar.blackout("AAPLm", datetime(2027, 1, 25, 15), 1, 1) is None
    assert calendar.next_report("AAPLm", datetime(2027, 1, 2)) == report


def test_the_shipped_earnings_file_reads_and_covers_the_traded_stocks():
    from datetime import datetime
    from pathlib import Path

    from tradebot.config import load_config
    from tradebot.earnings import EarningsCalendar

    root = Path(__file__).resolve().parents[1]
    calendar = EarningsCalendar(root / "config" / "earnings.json")
    config = load_config(root / "config" / "config.json", env={})
    stocks = [s for s in config.symbols if config.for_symbol(s).risk.market_hours and s != "ANT"]
    assert calendar.error is None or calendar.known("AAPL")
    assert all(calendar.known(s) for s in stocks), [s for s in stocks if not calendar.known(s)]
    assert calendar.error is None
    assert not calendar.known("ANT")  # no estimate exists yet: the dashboard says so
    assert calendar.next_report("NVDAm", datetime(2026, 11, 23)).isoformat() == "2027-02-24"
