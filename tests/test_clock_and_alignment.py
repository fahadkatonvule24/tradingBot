"""Time and data alignment between the broker, live trading and the backtester.

Found by checking the advisor dashboard against MT5: summer history was labelled an hour late,
the live trend filter used a different (and under-warmed) H4 EMA from the backtest, and the
countdown pointed at bar closes that could not trade.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from conftest import FakeMT5

from tradebot.advisor import next_entry_time
from tradebot.backtest import prepare_features, run_backtest
from tradebot.broker.clock import ServerClock, detect_convention, new_york_offset
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams, StrategyParams
from tradebot.data import load_trend_bars, preset_spec, synthetic_bars, trend_timeframes
from tradebot.engine import Engine, EntryPlan
from tradebot.live import HTF_HISTORY_MULTIPLE, latest_features
from tradebot.strategy import compute_signals, resample

NY_CLOSE = ServerClock(7, follows_new_york=True)  # MetaQuotes-style: New York + 7h


def server_time(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


# --- server clock ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("server", "utc"),
    [
        (datetime(2026, 1, 15, 14, 0), datetime(2026, 1, 15, 12, 0)),  # winter: UTC+2
        (datetime(2026, 7, 15, 15, 0), datetime(2026, 7, 15, 12, 0)),  # summer: UTC+3
        (datetime(2026, 3, 10, 15, 0), datetime(2026, 3, 10, 12, 0)),  # US DST began 8 Mar, EU not yet
    ],
)
def test_new_york_clock_converts_both_seasons(server, utc):
    assert NY_CLOSE.to_utc(pd.Series([pd.Timestamp(server)])).iloc[0] == utc
    epoch = NY_CLOSE.utc_to_server_epoch(utc)
    assert server_time(epoch) == server
    assert NY_CLOSE.epoch_to_utc(epoch) == utc


def test_fixed_clock_is_a_constant_shift():
    clock = ServerClock(2)
    assert (
        clock.offset_at(datetime(2026, 7, 1)) == clock.offset_at(datetime(2026, 1, 1)) == timedelta(hours=2)
    )
    assert "fixed" in clock.describe(datetime(2026, 1, 1))


def weekly_history(weeks: int, server_clock_of_utc) -> list[float]:
    """H1 bar open times (server epoch) for FX weeks that open Sunday 17:00 New York."""
    times = []
    for w in range(weeks):  # 17:00 New York wall-clock time every Sunday, across DST changes
        local = pd.Timestamp("2025-01-05 17:00") + pd.Timedelta(weeks=w)
        open_utc = local.tz_localize("America/New_York").tz_convert("UTC").tz_localize(None).to_pydatetime()
        for h in range(120):
            t = open_utc + timedelta(hours=h)
            times.append((t + server_clock_of_utc(t) - datetime(1970, 1, 1)).total_seconds())
    return times


def test_detects_a_server_that_follows_new_york():
    history = weekly_history(70, NY_CLOSE.offset_at)
    now = datetime(2026, 5, 5, 12)  # summer: measured offset +3
    clock = detect_convention(history, NY_CLOSE.offset_at(now), now)
    assert clock == ServerClock(7, follows_new_york=True)


def test_keeps_a_fixed_offset_server_fixed():
    history = weekly_history(70, lambda t: timedelta(hours=2))  # week opens drift by an hour
    now = datetime(2026, 5, 5, 12)
    assert detect_convention(history, timedelta(hours=2), now) == ServerClock(2)


def test_needs_both_seasons_to_decide():
    history = weekly_history(70, NY_CLOSE.offset_at)
    winter_only = [t for t in history if server_time(t).month in (12, 1, 2)]
    now = datetime(2026, 1, 5, 12)
    assert detect_convention(winter_only, timedelta(hours=2), now) == ServerClock(2)


def test_summer_bars_are_labelled_in_true_utc(fake_mt5):
    """Regression: a fixed +2h conversion put every summer bar one hour late."""
    broker = MT5Broker(MT5Login(), module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    broker._clock_rule = NY_CLOSE
    t = NY_CLOSE.utc_to_server_epoch(datetime(2025, 7, 4, 12, 0))
    assert broker._to_utc(t) == datetime(2025, 7, 4, 12, 0)
    assert broker._to_utc(NY_CLOSE.utc_to_server_epoch(datetime(2025, 12, 5, 13, 0))) == datetime(
        2025, 12, 5, 13
    )


def test_new_york_offset_follows_us_rules():
    assert new_york_offset(datetime(2026, 3, 9)) == timedelta(hours=-4)  # 2nd Sunday of March
    assert new_york_offset(datetime(2026, 11, 2)) == timedelta(hours=-5)  # 1st Sunday of November


# --- higher-timeframe alignment -------------------------------------------------------------------


def hourly(start: str, n: int) -> pd.DataFrame:
    t = pd.date_range(start, periods=n, freq="h")
    x = np.arange(n, dtype=float)
    return pd.DataFrame({"time": t, "open": x, "high": x + 1, "low": x - 1, "close": x})


def test_resampled_h4_bars_open_where_the_brokers_do():
    assert sorted(set(resample(hourly("2026-01-05", 48), 240)["time"].dt.hour)) == [2, 6, 10, 14, 18, 22]
    assert sorted(set(resample(hourly("2026-07-06", 48), 240)["time"].dt.hour)) == [1, 5, 9, 13, 17, 21]
    daily = resample(hourly("2026-07-06", 48), 1440)
    assert (daily["time"].dt.hour == 21).all()  # the FX day starts 17:00 New York


def test_live_fetches_enough_trend_history(fake_mt5):
    requested = {}
    original = fake_mt5.copy_rates_from_pos

    def spy(name, timeframe, start, count):
        requested[timeframe] = count
        return original(name, timeframe, start, count)

    fake_mt5.copy_rates_from_pos = spy
    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    config = BotConfig(symbols=("EURUSDm",), timeframe="M15")
    latest_features(broker, config, "EURUSDm")
    assert requested[FakeMT5.TIMEFRAME_H4] >= config.strategy.trend_ema * HTF_HISTORY_MULTIPLE
    # ...which is enough for the seed of the trend EMA to have no practical weight left:
    assert (1 - 2 / (config.strategy.trend_ema + 1)) ** (
        config.strategy.trend_ema * HTF_HISTORY_MULTIPLE
    ) < 1e-4


def test_backtest_uses_real_trend_bars_when_saved(tmp_path):
    config = BotConfig(
        symbols=("EURUSD",), timeframe="M15", schedule=ScheduleParams(session="all", news_file="")
    )
    bars = synthetic_bars(n=4000, seed=3)
    real_h4 = resample(synthetic_bars(n=4000, seed=99), 240)  # same times, different prices
    real_h4.to_csv(tmp_path / "EURUSD_H4.csv", index=False)
    assert trend_timeframes(config) == {"EURUSD": "H4"}
    htf = load_trend_bars(tmp_path, config)
    features = prepare_features(config, {"EURUSD": bars}, htf)["EURUSD"]
    expected = compute_signals(bars, config.strategy, "M15", htf[("EURUSD", "H4")])
    pd.testing.assert_series_equal(features["trend"], expected["trend"])
    assert not features["trend"].equals(compute_signals(bars, config.strategy, "M15")["trend"])
    run_backtest(config, {"EURUSD": bars}, {"EURUSD": preset_spec("EURUSD")}, htf=htf)  # plumbing works


def test_no_trend_bars_without_a_trend_filter(tmp_path):
    config = BotConfig(symbols=("EURUSD",), strategy=StrategyParams(use_trend_filter=False))
    assert trend_timeframes(config) == {} and load_trend_bars(tmp_path, config) == {}


# --- schedule -------------------------------------------------------------------------------------


def test_schedule_reasons_match_the_engine_wording():
    s = ScheduleParams(session="london_new_york")
    assert s.closed(datetime(2026, 11, 10, 5, 38)) == "05:00 UTC outside 07-21"
    assert s.closed(datetime(2026, 11, 10, 7, 0)) is None
    assert s.closed(datetime(2026, 11, 14, 12)) == "weekend"


def test_entries_resume_at_the_first_tradeable_bar_close():
    s = ScheduleParams(session="london_new_york")
    hour = timedelta(hours=1)
    assert next_entry_time(s, datetime(2026, 11, 10, 6), hour) == datetime(2026, 11, 10, 7)
    assert next_entry_time(s, datetime(2026, 11, 13, 21), hour) == datetime(2026, 11, 16, 7)  # Fri -> Mon


# --- exact P/L ------------------------------------------------------------------------------------


def test_plan_uses_the_terminals_profit_at_the_exact_prices(fake_mt5):
    """A pip on USDJPY is worth less at the target than at the stop; the terminal knows, a tick value doesn't."""
    calls = []

    def converted(kind, symbol, volume, price_open, price_close):
        calls.append((price_open, price_close))
        direction = 1 if kind == fake_mt5.ORDER_TYPE_BUY else -1
        return round((price_close - price_open) * direction * 100_000 * volume / price_close, 2)

    fake_mt5.order_calc_profit = converted
    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    config = BotConfig(
        symbols=("EURUSDm",), timeframe="M15", schedule=ScheduleParams(session="all", news_file="")
    )
    engine = Engine(config, broker, execute=False)
    spec = broker.symbol("EURUSDm")
    row = pd.Series({"atr": 0.0010, "raw_signal": 0, "signal": 0})
    plan = engine.plan_entry("EURUSDm", spec, config.for_symbol("EURUSDm"), row, 0.00012, -1)
    assert isinstance(plan, EntryPlan)
    entry = 1.10000  # sells fill at the bid
    stop, target = round(entry + 0.0015, 5), round(entry - 0.0030, 5)
    assert plan.risk_amount == pytest.approx(-converted(1, "EURUSDm", plan.volume, entry, stop), abs=0.02)
    assert plan.reward_amount == pytest.approx(converted(1, "EURUSDm", plan.volume, entry, target), abs=0.02)
    assert plan.risk_percent <= replace(config.risk).risk_percent + 1e-9


# --- losing and regaining the terminal ------------------------------------------------------------


def break_link(fake) -> None:
    """What MetaTrader5 does once the terminal is closed: every call fails with an IPC error."""
    fake.terminal_info = lambda: None
    fake.account_info = lambda: None
    fake.last_error = lambda: (-10001, "IPC send failed")
    fake.initialize = lambda **kwargs: False


def restore_link(fake) -> None:
    for name in ("terminal_info", "account_info", "last_error", "initialize"):
        delattr(fake, name)


def test_link_retries_with_backoff_and_logs_an_outage_once(fake_mt5, caplog):
    from tradebot.live import Link

    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    now = [0.0]
    tries = []
    original = broker.reconnect
    broker.reconnect = lambda: tries.append(now[0]) or original()
    link = Link(broker, clock=lambda: now[0])
    assert link.ok() and tries == []

    break_link(fake_mt5)
    with caplog.at_level("INFO", logger="tradebot.live"):
        for second in range(60):
            now[0] = float(second)
            assert not link.ok()
        assert tries == [0.0, 1.0, 3.0, 8.0, 18.0, 48.0]  # after 1, 2, 5, 10, then every 30 seconds
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1 and "IPC send failed (-10001)" in errors[0].getMessage()

        restore_link(fake_mt5)
        now[0] = 78.0
        assert link.ok()
        assert "reconnected to MetaTrader 5 after 78 s" in caplog.text


def test_dashboard_survives_a_terminal_restart(fake_mt5, caplog):
    """Regression: 'IPC send failed' used to print a traceback every second and never recover."""
    from tradebot.ui.monitor import LOST_LINK, Monitor

    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    config = BotConfig(
        symbols=("EURUSDm",), timeframe="M15", schedule=ScheduleParams(session="all", news_file="")
    )
    monitor = Monitor(config, broker, ["EURUSDm"])
    monitor.cycle()
    assert monitor.snapshot()["ready"] and monitor.snapshot()["error"] is None

    break_link(fake_mt5)
    with caplog.at_level("ERROR"):
        for _ in range(5):
            monitor.cycle()
    state = monitor.snapshot()
    assert state["error"] == LOST_LINK and state["symbols"]  # last good data stays on screen
    assert len(caplog.records) == 1 and "Traceback" not in caplog.text

    restore_link(fake_mt5)
    monitor._link._next_try = 0.0  # do not wait out the backoff in a test
    monitor.cycle()
    assert monitor.snapshot()["error"] is None


def test_other_failures_are_logged_once_not_every_second(fake_mt5, caplog):
    from tradebot.ui.monitor import Monitor

    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    config = BotConfig(
        symbols=("EURUSDm",), timeframe="M15", schedule=ScheduleParams(session="all", news_file="")
    )
    monitor = Monitor(config, broker, ["EURUSDm"])
    monitor.refresh = lambda: 1 / 0
    with caplog.at_level("ERROR"):
        for _ in range(4):
            monitor.cycle()
    assert len(caplog.records) == 1
    assert monitor.snapshot()["error"] == "ZeroDivisionError: division by zero"


def test_trading_loop_waits_for_the_terminal(fake_mt5):
    from tradebot.live import LiveRunner

    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    config = BotConfig(
        symbols=("EURUSDm",), timeframe="M15", schedule=ScheduleParams(session="all", news_file="")
    )
    runner = LiveRunner(config, broker, Engine(config, broker, execute=False, live=True), ["EURUSDm"])
    processed = []
    runner._process = processed.append
    break_link(fake_mt5)
    runner.cycle()
    assert processed == []
    restore_link(fake_mt5)
    runner._link._next_try = 0.0
    runner.cycle()
    assert processed == ["EURUSDm"]


def test_connect_waits_for_a_terminal_that_is_still_starting(fake_mt5):
    """Seen on the real terminal: 'Authorization failed' for the first seconds after it launches."""
    answers = [False, False, True]
    fake_mt5.initialize = lambda **kwargs: answers.pop(0)
    fake_mt5.last_error = lambda: (-6, "Terminal: Authorization failed")
    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    waits = []
    broker._sleep = waits.append
    assert broker.connect().balance == 10_000.0
    assert waits == [3.0, 3.0]

    fake_mt5.initialize = lambda **kwargs: False
    with pytest.raises(Exception, match=r"initialize failed \(-6\): Terminal: Authorization failed"):
        broker.connect(attempts=2)
    assert not broker.reconnect()  # a reconnect tries once; Link spaces the retries
