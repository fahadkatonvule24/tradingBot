"""Top-down analysis: two higher timeframes set the direction, the trading timeframe times the entry."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import SYMBOL, advice_for, call, features

from tradebot.advisor import filter_checks
from tradebot.broker.mt5 import MT5Broker
from tradebot.cli import build_parser
from tradebot.config import (
    BotConfig,
    ConfigError,
    MT5Login,
    ScheduleParams,
    StrategyParams,
    auto_trend_timeframes,
    config_from_dict,
)
from tradebot.data import load_trend_bars, trend_timeframes
from tradebot.live import latest_features
from tradebot.strategy import compute_signals, resample
from tradebot.ui.monitor import SWITCHABLE, Monitor
from tradebot.ui.server import TOKEN_HEADER, DashboardServer

NOW = datetime(2026, 3, 4, 12, 7)
AUTO = StrategyParams(trend_timeframes="auto")


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
        strategy=AUTO,
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


# --- which timeframes -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trading", "higher"),
    [
        ("M5", ("H1", "H4")),
        ("M15", ("H1", "H4")),
        ("M30", ("H1", "H4")),
        ("H1", ("H4", "D1")),
        ("H4", ("D1",)),
    ],
)
def test_auto_picks_the_two_higher_timeframes(trading, higher):
    assert AUTO.trend_ladder(trading) == higher


def test_ladder_can_be_set_by_hand_or_left_as_the_single_filter():
    assert StrategyParams(trend_timeframes=["M30", "H4"]).trend_ladder("M5") == ("M30", "H4")
    assert StrategyParams().trend_ladder("M5") == ("H4",)  # the pre-ladder behaviour
    assert auto_trend_timeframes("M10") == ("M30", "H1")  # non-standard: the next two at least 3x larger
    assert auto_trend_timeframes("H2") == ("D1",)


def test_config_accepts_auto_and_rejects_unknown_timeframes():
    ok = config_from_dict(
        {"symbols": ["EURUSD"], "timeframe": "M30", "strategy": {"trend_timeframes": "auto"}}, env={}
    )
    assert ok.strategy.trend_ladder(ok.timeframe) == ("H1", "H4")
    with pytest.raises(ConfigError, match="trend_timeframes"):
        config_from_dict({"symbols": ["EURUSD"], "strategy": {"trend_timeframes": ["H1", "H7"]}}, env={})
    with pytest.raises(ConfigError, match="trend_timeframes"):
        config_from_dict({"symbols": ["EURUSD"], "strategy": {"trend_timeframes": "automatic"}}, env={})


# --- signals --------------------------------------------------------------------------------------


def test_both_higher_timeframes_must_agree(bars):
    p = StrategyParams(trend_timeframes=("H1", "H4"), trend_ema=50)
    out = compute_signals(bars, p, "M15")
    h1, h4, both = out["trend_H1"], out["trend_H4"], out["trend"]
    assert ((h1 != 0) & (h1 != h4)).sum() > 0  # the two do disagree in places
    assert (both[h1 == h4] == h1[h1 == h4]).all() and (both[h1 != h4] == 0).all()
    taken = out[out["signal"] != 0]
    assert len(taken) > 0
    assert ((taken["signal"] == taken["trend_H1"]) & (taken["signal"] == taken["trend_H4"])).all()
    fewer = (compute_signals(bars, replace(p, trend_timeframes=("H1",)), "M15")["signal"] != 0).sum()
    assert len(taken) <= fewer  # a second filter can only remove trades


def test_ladder_is_causal_and_the_single_filter_is_unchanged(bars):
    p = StrategyParams(trend_timeframes=("H1", "H4"), trend_ema=50)
    full = compute_signals(bars, p, "M15")
    prefix = compute_signals(bars.iloc[:3333], p, "M15")
    columns = ["signal", "trend", "trend_H1", "trend_H4"]
    pd.testing.assert_frame_equal(prefix[columns], full[columns].iloc[:3333], check_dtype=False)

    legacy = compute_signals(bars, StrategyParams(), "M15")
    explicit = compute_signals(bars, StrategyParams(trend_timeframes=("H4",)), "M15")
    pd.testing.assert_series_equal(legacy["signal"], explicit["signal"])
    pd.testing.assert_series_equal(legacy["trend"], legacy["trend_H4"], check_names=False)


def test_real_bars_are_used_per_timeframe(bars):
    p = StrategyParams(trend_timeframes=("H1", "H4"), trend_ema=50)
    other_h4 = resample(bars, 240).assign(close=lambda d: d["close"][::-1].to_numpy())  # different H4 closes
    swapped = compute_signals(bars, p, "M15", {"H4": other_h4})
    plain = compute_signals(bars, p, "M15")
    pd.testing.assert_series_equal(swapped["trend_H1"], plain["trend_H1"])  # H1 still resampled
    assert not swapped["trend_H4"].equals(plain["trend_H4"])


def test_history_files_for_every_higher_timeframe(tmp_path, bars, cfg):
    config = replace(cfg, symbols=("EURUSD",))
    assert trend_timeframes(config) == {"EURUSD": ("H1", "H4")}
    resample(bars, 60).assign(spread=0).to_csv(tmp_path / "EURUSD_H1.csv", index=False)
    assert set(load_trend_bars(tmp_path, config)) == {("EURUSD", "H1")}  # H4 is missing: it will be resampled


def test_live_fetches_both_higher_timeframes(cfg, broker, fake):
    asked = []
    original = fake.copy_rates_from_pos
    fake.copy_rates_from_pos = lambda name, tf, start, count: (
        asked.append(tf) or original(name, tf, start, count)
    )
    frame = latest_features(broker, cfg, SYMBOL)
    assert {FakeMT5.TIMEFRAME_H1, FakeMT5.TIMEFRAME_H4} <= set(asked)
    assert {"trend_H1", "trend_H4"} <= set(frame.columns)


# --- advice ---------------------------------------------------------------------------------------


def laddered(h1: int, h4: int, **last) -> pd.DataFrame:
    frame = features({"trend": h1 if h1 == h4 else 0, **last})
    frame["trend_H1"], frame["trend_H4"] = h1, h4
    return frame


def test_checklist_has_a_row_per_higher_timeframe():
    rows = {c.name: c.ok for c in filter_checks(laddered(1, -1), AUTO, side=1)}
    assert rows["H1 trend"] is True and rows["H4 trend"] is False
    assert "H4 trend" in {
        c.name for c in filter_checks(features(), StrategyParams(), side=1)
    }  # single filter


def test_advice_explains_agreement_and_disagreement(cfg, broker):
    down = advice_for(cfg, broker, laddered(-1, -1))
    assert down.side == -1
    assert down.direction_note.startswith(
        "Only sells are considered here: the H1 and H4 trends are down "
        "(the last completed H1 and H4 bars closed below their EMA 200)."
    )
    assert "until the H1 and H4 trends turn." in down.direction_note

    split = advice_for(cfg, broker, laddered(1, -1))
    assert split.direction_note == (
        "No trades either way right now: the higher timeframes disagree (H1 up, H4 down). "
        "A trigger is only taken when H1 and H4 point the same way."
    )


# --- switching the trading timeframe on the dashboard ---------------------------------------------


def test_switching_timeframe_reanalyses_on_the_new_bars(cfg, broker):
    monitor = Monitor(cfg, broker, [SYMBOL])
    monitor.refresh()
    state = monitor.snapshot()
    assert (state["timeframe"], state["trend_timeframes"]) == ("M15", ["H1", "H4"])
    assert state["timeframes"] == list(SWITCHABLE)
    assert state["symbols"][0]["advice"]["decision_at"] == "2026-03-04T12:15:00Z"

    result = monitor._apply_timeframe("M30")
    assert result == {
        "ok": True,
        "message": "Now analysing M30 (direction from H1 + H4).",
        "timeframe": "M30",
    }
    assert monitor.chart(SYMBOL) is None  # old bars are dropped at once
    monitor.refresh()
    state = monitor.snapshot()
    assert state["timeframe"] == "M30" and monitor.engine.config.timeframe == "M30"
    assert monitor.chart(SYMBOL)["bars"][-1]["time"] == "2026-03-04T11:30:00Z"
    assert state["symbols"][0]["advice"]["decision_at"] == "2026-03-04T12:30:00Z"
    json.dumps(state, allow_nan=False)

    assert monitor._apply_timeframe("H4")["message"] == "Now analysing H4 (direction from D1)."
    assert not monitor._apply_timeframe("W1")["ok"] and monitor.config.timeframe == "H4"


def test_switch_goes_through_the_worker_thread(cfg, broker):
    monitor = Monitor(cfg, broker, [SYMBOL], interval=0.05)
    worker = threading.Thread(target=monitor.run, daemon=True)
    worker.start()
    try:
        assert monitor.set_timeframe("m5", timeout=10)["timeframe"] == "M5"
    finally:
        monitor.stop()
        worker.join(5)
    assert monitor.config.timeframe == "M5"


def test_requests_are_served_even_when_every_refresh_overruns(cfg, broker):
    """Regression: with slow refreshes the page's buttons (timeframe, orders) were never handled."""
    import time

    monitor = Monitor(cfg, broker, [SYMBOL], interval=0.01)
    monitor.cycle = lambda: time.sleep(0.05)  # always longer than the interval
    worker = threading.Thread(target=monitor.run, daemon=True)
    worker.start()
    try:
        assert monitor.set_timeframe("M30", timeout=5) == {
            "ok": True, "message": "Now analysing M30 (direction from H1 + H4).", "timeframe": "M30"
        }  # fmt: skip
    finally:
        monitor.stop()
        worker.join(5)


def test_preview_reuses_the_higher_timeframe_bars_already_fetched(cfg, broker, fake):
    from tradebot.live import fetch_frames, preview_features

    bars, htf = fetch_frames(broker, cfg, SYMBOL)
    closed = compute_signals(bars, cfg.strategy, cfg.timeframe, htf)
    sizes = []
    original = fake.copy_rates_from_pos
    fake.copy_rates_from_pos = lambda name, tf, start, count: (
        sizes.append(count) or original(name, tf, start, count)
    )
    preview = preview_features(broker, cfg, SYMBOL, closed, htf)
    assert len(preview) == len(closed) + 1
    assert max(sizes) == 1  # only the forming bars are requested, never the history again


class Stub:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def set_timeframe(self, timeframe):
        self.asked.append(timeframe)
        return {"ok": timeframe != "W1", "message": "ok", "timeframe": timeframe}


@pytest.fixture
def server():
    srv = DashboardServer(Stub(), port=0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_timeframe_endpoint_needs_the_token(server):
    assert call(server, "POST", "/api/timeframe", {"timeframe": "M30"})[0] == 403
    assert server.monitor.asked == []
    status, data, _ = call(
        server, "POST", "/api/timeframe", {"timeframe": "M30"}, {TOKEN_HEADER: server.token}
    )
    assert status == 200 and json.loads(data)["timeframe"] == "M30"
    assert call(server, "POST", "/api/timeframe", {"timeframe": "W1"}, {TOKEN_HEADER: server.token})[0] == 400
    assert call(server, "POST", "/api/timeframe", {}, {TOKEN_HEADER: server.token})[0] == 400
    assert server.monitor.asked == ["M30", "W1"]


def test_command_line_can_start_on_a_timeframe():
    parser = build_parser()
    assert parser.parse_args(["ui", "--timeframe", "m30"]).timeframe == "M30"
    assert parser.parse_args(["run", "--timeframe", "m5"]).timeframe == "M5"
    assert parser.parse_args(["fetch", "--timeframe", "m30"]).timeframe == "M30"
