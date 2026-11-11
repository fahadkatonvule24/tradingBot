"""Break and retest: a level breaks, price comes back to it, and a bar closes away from it again."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import SYMBOL, advice_for, call, features

from tradebot.backtest import run_backtest
from tradebot.broker.mt5 import MT5Broker
from tradebot.cli import build_parser
from tradebot.config import STRATEGIES, BotConfig, ConfigError, MT5Login, ScheduleParams, StrategyParams
from tradebot.strategy import FILTERS, compute_signals, explain, retest_setup
from tradebot.ui.monitor import Monitor
from tradebot.ui.server import TOKEN_HEADER, DashboardServer

NOW = datetime(2026, 3, 4, 12, 7)
P = StrategyParams(name="retest", retest_period=5, retest_window=4, retest_tolerance_atr=0.25)
LEVEL = 1.1010  # the high of the quiet bars below


def frame(*tail: tuple[float, float, float, float], quiet: int = 6) -> pd.DataFrame:
    """``quiet`` flat bars (high 1.1010, low 1.0990), then the given (open, high, low, close) bars."""
    ohlc = [(1.1000, 1.1010, 1.0990, 1.1000)] * quiet + list(tail)
    start = datetime(2026, 3, 4, 8, 0)
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"])
    df.insert(0, "time", [start + timedelta(minutes=15 * i) for i in range(len(df))])
    df["atr"] = 0.0010  # tolerance = 0.25 ATR = 2.5 pips
    return df


BREAK_UP = (1.1002, 1.1022, 1.1001, 1.1020)  # closes above the 5-bar high
DRIFT = (1.1020, 1.1026, 1.1016, 1.1022)  # stays above the level without touching it
RETEST = (1.1018, 1.1024, 1.1011, 1.1021)  # dips to within 2.5 pips of the level, closes up above it


def test_break_then_retest_triggers_a_buy_on_the_retest_bar():
    out = retest_setup(frame(BREAK_UP, DRIFT, RETEST), P)
    assert out["trigger"].tolist() == [0] * 8 + [1]
    # after the break the level is watched, with the bars left counting down; the trigger clears it
    assert out["retest_side"].tolist()[6:] == [1, 1, 0]
    assert out["retest_level"].iloc[6] == pytest.approx(LEVEL)
    assert out["retest_left"].tolist()[6:8] == [4, 3]


def test_the_break_itself_is_not_a_trigger_and_a_down_bar_at_the_level_is_not_a_hold():
    falling_into_level = (1.1022, 1.1023, 1.1011, 1.1012)  # touches the level but closes down
    out = retest_setup(frame(BREAK_UP, falling_into_level), P)
    assert not out["trigger"].any()
    assert out["retest_side"].iloc[-1] == 1  # still watching


def test_a_close_back_through_the_level_cancels_the_setup():
    failed = (1.1018, 1.1019, 1.1000, 1.1004)  # closes 6 pips back inside
    out = retest_setup(frame(BREAK_UP, failed, RETEST), P)
    assert not out["trigger"].any()
    assert out["retest_side"].tolist()[6:] == [1, 0, 0]


def test_the_setup_expires_after_the_window():
    out = retest_setup(frame(BREAK_UP, DRIFT, DRIFT, DRIFT, DRIFT, RETEST), P)
    assert not out["trigger"].any()  # the retest came on the fifth bar after the break; window is 4
    assert out["retest_left"].tolist()[6:] == [4, 3, 2, 1, 0, 0]


def test_sell_is_the_mirror_image():
    def mirror(o, h, low, c):
        return (2.2 - o, 2.2 - low, 2.2 - h, 2.2 - c)

    out = retest_setup(frame(mirror(*BREAK_UP), mirror(*DRIFT), mirror(*RETEST)), P)
    assert out["trigger"].tolist() == [0] * 8 + [-1]
    assert out["retest_level"].iloc[6] == pytest.approx(1.0990)


def test_signals_carry_the_watched_level_and_only_trend_and_adx_filter_it(bars):
    assert FILTERS["retest"] == ("trend", "adx")
    df = compute_signals(bars, StrategyParams(name="retest"), "M15")
    assert {"retest_side", "retest_level", "retest_left"} <= set(df.columns)
    assert set(df.loc[df["raw_signal"] != 0, "block"]) <= {"", "trend", "adx", "warmup"}
    watching = df[df["retest_side"] != 0]
    assert len(watching) and watching["retest_level"].notna().all()
    assert "retest_side" not in compute_signals(bars, StrategyParams(), "M15").columns


@pytest.mark.parametrize("name", STRATEGIES)
def test_every_strategy_explains_its_signal_and_backtests(bars, config, eurusd, name):
    p = StrategyParams(name=name, use_trend_filter=False)
    df = compute_signals(bars, p, "M15")
    row = df[df["signal"] != 0].iloc[0]
    assert explain(row, p)
    result = run_backtest(config.with_strategy(name=name), {"EURUSD": bars}, {"EURUSD": eurusd})
    assert result.metrics.trades > 0


def test_config_validates_the_retest_settings():
    with pytest.raises(ConfigError, match="retest_period"):
        BotConfig(symbols=(SYMBOL,), strategy=StrategyParams(name="retest", retest_window=0)).validate()


# --- advice ---------------------------------------------------------------------------------------


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
        strategy=StrategyParams(name="retest"),
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


def test_advice_names_the_broken_level_being_watched(cfg, broker):
    watching = features({"retest_side": 1, "retest_level": 1.0995, "retest_left": 5, "trend": 0})
    a = advice_for(cfg, broker, watching)
    assert (a.action, a.side) == ("WAIT", 1)
    assert a.trigger_level == pytest.approx(1.0995)
    assert "Price broke above 1.09950" in a.trigger and "5 more bars" in a.trigger


def test_advice_says_what_starts_a_setup_when_nothing_has_broken(cfg, broker):
    idle = features({"retest_side": 0, "retest_level": float("nan"), "retest_left": 0})
    a = advice_for(cfg, broker, idle)
    assert a.trigger_level == pytest.approx(idle["high"].tail(20).max())
    assert "A buy setup starts when a M15 bar closes above" in a.trigger
    against = advice_for(cfg, broker, features({"retest_side": -1, "retest_level": 1.0980, "retest_left": 3}))
    assert against.side == 1 and against.trigger.startswith("The last break was the other way (down).")


# --- the setup switch -----------------------------------------------------------------------------


def test_switching_setup_reanalyses_with_the_new_trigger(cfg, broker):
    monitor = Monitor(cfg, broker, [SYMBOL])
    monitor.refresh()
    state = monitor.snapshot()
    assert state["strategy"] == "retest" and state["strategies"] == list(STRATEGIES)

    assert monitor._apply_strategy("breakout") == {
        "ok": True, "message": "Now watching the breakout setup.", "strategy": "breakout"
    }  # fmt: skip
    assert monitor.chart(SYMBOL) is None  # the old analysis is dropped at once
    monitor.refresh()
    state = monitor.snapshot()
    assert state["strategy"] == "breakout" and monitor.engine.config.strategy.name == "breakout"
    assert "closes" in state["symbols"][0]["advice"]["trigger"]
    json.dumps(state, allow_nan=False)

    refused = monitor._apply_strategy("martingale")
    assert not refused["ok"] and monitor.config.strategy.name == "breakout"


def test_switching_setup_applies_to_symbols_with_their_own_strategy_override(cfg, broker):
    from dataclasses import replace

    cfg = replace(cfg, overrides={SYMBOL: {"strategy": {"name": "pullback", "adx_min": 15.0}}})
    monitor = Monitor(cfg, broker, [SYMBOL])
    assert monitor._apply_strategy("ema_cross")["ok"]
    params = monitor.config.for_symbol(SYMBOL).strategy
    assert (params.name, params.adx_min) == ("ema_cross", 15.0)  # other overrides are kept


def test_setup_switch_goes_through_the_worker_thread(cfg, broker):
    monitor = Monitor(cfg, broker, [SYMBOL], interval=0.01)
    worker = threading.Thread(target=monitor.run, daemon=True)
    worker.start()
    try:
        assert monitor.set_strategy("EMA_CROSS", timeout=5)["strategy"] == "ema_cross"
        assert monitor.set_timeframe("M30", timeout=5)["timeframe"] == "M30"  # both switches still work
    finally:
        monitor.stop()
        worker.join(5)
    assert (monitor.config.strategy.name, monitor.config.timeframe) == ("ema_cross", "M30")


class Stub:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def set_strategy(self, name):
        self.asked.append(name)
        return {"ok": name in STRATEGIES, "message": "ok", "strategy": name}


@pytest.fixture
def server():
    srv = DashboardServer(Stub(), port=0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_strategy_endpoint_needs_the_token(server):
    assert call(server, "POST", "/api/strategy", {"strategy": "retest"})[0] == 403
    assert server.monitor.asked == []
    auth = {TOKEN_HEADER: server.token}
    status, data, _ = call(server, "POST", "/api/strategy", {"strategy": "retest"}, auth)
    assert status == 200 and json.loads(data)["strategy"] == "retest"
    assert call(server, "POST", "/api/strategy", {"strategy": "martingale"}, auth)[0] == 400
    assert call(server, "POST", "/api/strategy", {"timeframe": "M30"}, auth)[0] == 400
    assert server.monitor.asked == ["retest", "martingale"]


def test_command_line_can_start_on_a_setup():
    parser = build_parser()
    assert parser.parse_args(["ui", "--strategy", "retest"]).strategy == "retest"
    assert parser.parse_args(["run", "--strategy", "breakout"]).strategy == "breakout"
    with pytest.raises(SystemExit):
        parser.parse_args(["ui", "--strategy", "martingale"])
