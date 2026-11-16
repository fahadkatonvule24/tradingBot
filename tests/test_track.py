"""The setup's record on stored history, measured in a separate process (tradebot.ui.track).

Regression for a real complaint (16 Nov 2026): clicks on the dashboard were slow. Every setup or
timeframe switch ran an 11-second backtest inside the dashboard's own process; four switches in a
minute kept a core busy, and with the page competing for the interpreter one run took 96 s.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from test_advisor_ui import SYMBOL, features, primed
from test_alerts import broker, cfg, fake  # noqa: F401  (fixtures)

from tradebot.config import BotConfig, ScheduleParams
from tradebot.data import synthetic_bars
from tradebot.ui.monitor import Monitor
from tradebot.ui.track import NoRecord, TrackRecords

QUICK_RECORD = '{"trades": 7, "win_rate": 42.0, "start": "2016-08-29T06:00:00Z", "symbols": {}}'


def printing(record: str = QUICK_RECORD, calls: list | None = None):
    """A child that prints a record at once, counting how often one was started."""

    def command(config_path: Path, data_dir, names) -> list[str]:
        if calls is not None:
            calls.append(config_path)
        return [sys.executable, "-c", f"import sys; sys.stdout.write({record!r})"]

    return command


def test_a_record_is_measured_once_per_setup_and_timeframe():
    calls: list = []
    tracks = TrackRecords(printing(calls=calls))
    retest = BotConfig(timeframe="H1").with_strategy(name="retest")
    assert tracks.cached(retest) is None
    first = tracks.get(retest, "data", {})
    assert first["trades"] == 7 and tracks.cached(retest) == first
    assert tracks.get(retest, "data", {}) == first and len(calls) == 1  # switching back: no new run
    tracks.get(retest.with_strategy(name="pullback"), "data", {})
    tracks.get(BotConfig(timeframe="M15").with_strategy(name="retest"), "data", {})
    assert len(calls) == 3


def test_a_newer_switch_stops_the_run_nobody_is_waiting_for():
    def slow_then_quick(config_path, data_dir, names):
        if "slow" in str(data_dir):
            return [sys.executable, "-c", "import time; time.sleep(60)"]
        return printing()(config_path, data_dir, names)

    tracks = TrackRecords(slow_then_quick)
    outcome: dict = {}

    def first() -> None:
        try:
            tracks.get(BotConfig().with_strategy(name="retest"), "slow", {})
        except NoRecord as exc:
            outcome["first"] = str(exc)

    started = time.monotonic()
    thread = threading.Thread(target=first)
    thread.start()
    while tracks._process is None and time.monotonic() - started < 10:
        time.sleep(0.05)
    assert tracks.get(BotConfig().with_strategy(name="pullback"), "quick", {})["trades"] == 7
    thread.join(20)
    assert "stopped" in outcome["first"] and time.monotonic() - started < 30
    assert tracks.cached(BotConfig().with_strategy(name="retest")) is None  # a stopped run caches nothing


def test_the_real_child_measures_stored_history(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    synthetic_bars(n=3000, seed=8).to_csv(data / "EURUSD_M15.csv", index=False)
    config = BotConfig(symbols=("EURUSD",), timeframe="M15",
                       schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""))  # fmt: skip
    # synthetic bars give the breakout setup no trades once the trend filter reads them
    breakout = config.with_strategy(name="breakout", use_trend_filter=False)
    record = TrackRecords().get(breakout, data, {"EURUSDm": "EURUSD"})
    assert record["trades"] > 0 and set(record["symbols"]) == {"EURUSDm"}
    record = TrackRecords().get(config, data, {"EURUSDm": "EURUSD"})
    assert record["start"].endswith("Z")  # JSON-safe, as the page needs it


def test_missing_history_is_no_record_not_a_crash(tmp_path):
    with pytest.raises(NoRecord, match="no track record"):
        TrackRecords().get(BotConfig(symbols=("EURUSD",), timeframe="M15"), tmp_path, {"EURUSD": "EURUSD"})


def test_a_switch_to_a_measured_setup_shows_its_record_at_once(cfg, broker):  # noqa: F811
    monitor = primed(Monitor(cfg, broker, [SYMBOL]), features())
    monitor.tracks = TrackRecords(printing())
    monitor.load_track_record("data", {SYMBOL: "EURUSD"})  # ema_cross, measured now
    assert monitor.track_record["trades"] == 7 and not monitor._track_pending
    assert monitor._apply_strategy("breakout")["ok"]
    assert monitor._track_pending  # breakout has not been measured: it runs in the background
    deadline = time.monotonic() + 20
    while monitor._track_pending and time.monotonic() < deadline:
        time.sleep(0.05)
    assert monitor._apply_strategy("ema_cross")["ok"]
    assert monitor.track_record["trades"] == 7 and not monitor._track_pending  # from the cache
