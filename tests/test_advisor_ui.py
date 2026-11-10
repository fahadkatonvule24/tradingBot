"""The advisor dashboard: advice logic, the MT5 worker, and the local web server's safeguards."""

from __future__ import annotations

import http.client
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
import pytest
from conftest import FakeMT5, bar

from tradebot.advisor import advise, filter_checks, pip_size, recent_signals
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams, StrategyParams
from tradebot.engine import Engine, EntryPlan
from tradebot.ui.monitor import Monitor, clean
from tradebot.ui.server import TOKEN_HEADER, DashboardServer

SYMBOL = "EURUSDm"
NOW = datetime(2026, 3, 4, 12, 7)  # FakeMT5's clock: a Wednesday, 7 minutes into the 12:00 bar


@pytest.fixture
def fake() -> FakeMT5:
    return FakeMT5(now=NOW)


@pytest.fixture
def broker(fake: FakeMT5) -> MT5Broker:
    return MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)


@pytest.fixture
def cfg() -> BotConfig:
    return BotConfig(
        symbols=(SYMBOL,),
        timeframe="M15",
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


def features(last: dict | None = None, n: int = 30) -> pd.DataFrame:
    """Closed M15 bars ending with the 11:45 bar; ``last`` overrides columns of the final row."""
    rows = []
    for i in range(n):
        t = datetime(2026, 3, 4, 11, 45) - timedelta(minutes=15 * (n - 1 - i))  # last bar closes 12:00
        price = 1.0990 + i * 0.00002
        rows.append(bar(t, price, price + 0.0004, price - 0.0004, price, ema_fast=1.0995, ema_slow=1.0990,
                        macd_hist=0.0002, resistance=1.1050, support=1.0900))  # fmt: skip
    df = pd.DataFrame(rows)
    for key, value in (last or {}).items():
        df.loc[df.index[-1], key] = value
    return df


def advice_for(cfg, broker, frame, now=NOW):
    engine = Engine(cfg, broker, execute=False, live=True)
    spec = broker.symbol(SYMBOL)
    return advise(engine, SYMBOL, spec, cfg.for_symbol(SYMBOL), frame, broker.tick(SYMBOL), now)


# --- advice ---------------------------------------------------------------------------------------


def test_buy_signal_is_actionable_and_priced_at_the_live_ask(cfg, broker):
    a = advice_for(cfg, broker, features({"signal": 1, "raw_signal": 1}))
    assert (a.action, a.actionable, a.side) == ("BUY", True, 1)
    p = a.plan
    # ask 1.10012; ATR 0.0010 -> stop 1.5 ATR, target 3 ATR; 0.25% of 10,000 = 25 -> 0.16 lots at $10/pip
    assert (p.entry, p.stop, p.target) == (1.10012, 1.09862, 1.10312)
    assert p.volume == 0.16
    assert p.risk_amount == pytest.approx(24.0)
    assert p.reward_amount == pytest.approx(48.0)
    assert p.reward_risk == pytest.approx(2.0)
    assert a.decision_at == datetime(2026, 3, 4, 12, 15)


def test_signal_outside_the_session_is_a_skip_not_a_trade(cfg, broker):
    cfg = replace(cfg, schedule=replace(cfg.schedule, session="asia"))  # 23:00-08:00 UTC
    a = advice_for(cfg, broker, features({"signal": 1, "raw_signal": 1}))
    assert a.action == "SKIP" and not a.actionable
    assert "session" in a.detail


def test_engine_size_rejection_also_skips(cfg, broker):
    cfg = replace(cfg, risk=replace(cfg.risk, risk_percent=0.01))  # $1 risk is below the minimum lot
    a = advice_for(cfg, broker, features({"signal": -1, "raw_signal": -1}))
    assert a.action == "SKIP"
    assert "minimum lot" in a.detail and a.plan.volume == 0


def test_filtered_trigger_says_which_filter(cfg, broker):
    a = advice_for(cfg, broker, features({"raw_signal": 1, "block": "adx"}))
    assert a.action == "WAIT" and a.headline == "Buy trigger filtered out"
    assert "ADX below minimum" in a.detail


def test_no_setup_explains_the_next_trigger(cfg, broker):
    # fast EMA above slow with an up trend: the next buy needs a fresh cross
    a = advice_for(cfg, broker, features())
    assert a.action == "WAIT" and a.side == 1
    assert "fresh cross" in a.trigger
    down = advice_for(cfg, broker, features({"trend": -1}))
    assert down.side == -1 and "closes below EMA 50" in down.trigger


def test_stale_bars_mean_market_closed(cfg, broker, fake):
    fake.utc_now = NOW + timedelta(hours=2)
    a = advice_for(cfg, broker, features(), now=fake.utc_now)
    assert a.action == "CLOSED" and a.plan is None and a.decision_at is None


def test_breakout_trigger_has_a_price_level(cfg, broker):
    cfg = cfg.with_strategy(name="breakout", breakout_period=20)
    frame = features()
    a = advice_for(cfg, broker, frame)
    assert a.trigger_level == pytest.approx(frame["high"].tail(20).max())
    assert "closes above" in a.trigger


def test_filter_checks_follow_the_strategy():
    p = StrategyParams()
    frame = features({"rsi": 75.0})
    names = {c.name: c.ok for c in filter_checks(frame, p, side=1)}
    assert names == {"H4 trend": True, "Trend strength": True, "Momentum": True,
                     "Not overextended": False, "Room to run": True}  # fmt: skip
    assert not any(c.ok for c in filter_checks(frame, p, side=-1) if c.name in ("H4 trend", "Momentum"))


def test_plan_entry_is_what_the_engine_trades(cfg, broker):
    engine = Engine(cfg, broker, execute=False)
    row = features({"signal": 1, "raw_signal": 1}).iloc[-1]
    plan = engine.plan_entry(SYMBOL, broker.symbol(SYMBOL), cfg.for_symbol(SYMBOL), row, 0.00012, 1)
    assert isinstance(plan, EntryPlan)
    assert plan.risk_percent == pytest.approx(0.24)
    assert "EMA 10/50 cross up" in plan.reason


def test_pip_size():
    from tradebot.data import preset_spec

    assert pip_size(preset_spec("EURUSD")) == pytest.approx(0.0001)
    assert pip_size(preset_spec("XAUUSD")) == pytest.approx(0.01)


# --- recent signals -------------------------------------------------------------------------------


def history(frame, cfg, **schedule):
    sched = replace(cfg.schedule, **schedule) if schedule else cfg.schedule
    return recent_signals(frame, cfg.for_symbol(SYMBOL), sched, cfg.bar_minutes)


def test_recent_signal_outcomes(cfg):
    """Entry 1.0992 at bar 10: stop 1.0977 (1.5 ATR), target 1.1022 (3 ATR)."""
    frame = features()
    frame.loc[10, "signal"] = 1
    (still_open,) = history(frame, cfg)["items"]
    assert still_open["outcome"] == "open" and still_open["r"] == pytest.approx(0.2533, abs=1e-3)
    assert (still_open["stop"], still_open["target"]) == (pytest.approx(1.0977), pytest.approx(1.1022))

    frame.loc[15, "high"] = 1.1030
    (hit,) = history(frame, cfg)["items"]
    assert (hit["outcome"], hit["r"]) == ("target", pytest.approx(2.0))

    frame.loc[12, "low"] = 1.0970  # the stop is touched three bars earlier
    assert history(frame, cfg)["items"][0]["outcome"] == "stop"

    both = features()
    both.loc[10, "signal"] = -1  # sell: stop 1.1007, target 1.0962
    both.loc[11, ["high", "low"]] = [1.1010, 1.0960]
    item = history(both, cfg)["items"][0]
    assert (item["outcome"], item["r"]) == ("stop", -1.0)  # one bar touching both counts as the stop


def test_recent_signals_newest_first_and_schedule_aware(cfg):
    frame = features()
    frame.loc[[5, 20], "signal"] = [1, -1]
    out = history(frame, cfg)
    assert [i["side"] for i in out["items"]] == [-1, 1] and out["tradeable"] == 2
    assert all(i["blocked"] is None for i in out["items"])
    asia = history(frame, cfg, session="asia")  # 23:00-08:00 UTC; these bars close around 06:00 and 09:45
    assert asia["tradeable"] == 1
    assert asia["items"][0]["blocked"] == "09:00 UTC outside 23-08" and asia["items"][1]["blocked"] is None


def test_snapshot_includes_signal_history(cfg, broker):
    monitor = Monitor(cfg, broker, [SYMBOL])
    monitor.refresh()
    row = monitor.snapshot()["symbols"][0]
    assert set(row["history"]) == {"items", "tradeable", "weeks"}
    json.dumps(row["history"], allow_nan=False)


# --- monitor --------------------------------------------------------------------------------------


def live_cfg(cfg: BotConfig) -> BotConfig:
    return replace(cfg, dry_run=False, live_trading_confirm=True)


def primed(monitor: Monitor, frame: pd.DataFrame) -> Monitor:
    monitor._features[SYMBOL] = frame
    monitor._fetched[SYMBOL] = time.monotonic()
    return monitor


def test_snapshot_is_strict_json(cfg, broker):
    monitor = Monitor(cfg, broker, [SYMBOL])
    monitor.refresh()
    state = monitor.snapshot()
    json.dumps(state, allow_nan=False)
    row = state["symbols"][0]
    assert state["gate"] == {"execute": False, "reason": "dry_run is true in the config", "live_flag": False}
    assert row["advice"]["action"] in {"BUY", "SELL", "WAIT", "SKIP"}
    assert row["forming"]["time"] == "2026-03-04T12:00:00Z"
    assert monitor.chart(SYMBOL)["bars"][-1]["time"] == "2026-03-04T11:45:00Z"


def test_orders_refused_while_the_gate_is_closed(cfg, broker, fake):
    monitor = primed(Monitor(cfg, broker, [SYMBOL], live_flag=True), features({"signal": 1, "raw_signal": 1}))
    result = monitor.execute(SYMBOL, 1)
    assert not result["ok"] and "dry_run" in result["message"]
    assert fake.sent == []


def test_order_needs_the_live_flag_too(cfg, broker, fake):
    monitor = primed(Monitor(live_cfg(cfg), broker, [SYMBOL]), features({"signal": 1, "raw_signal": 1}))
    assert "--live" in monitor.execute(SYMBOL, 1)["message"]
    assert fake.sent == []


def test_order_sends_exactly_the_advised_trade(cfg, broker, fake, tmp_path):
    config = live_cfg(cfg)
    monitor = primed(
        Monitor(config, broker, [SYMBOL], live_flag=True), features({"signal": 1, "raw_signal": 1})
    )
    result = monitor.execute(SYMBOL, 1)
    assert result["ok"], result
    (order,) = fake.sent
    assert (order["volume"], order["sl"], order["tp"], order["magic"]) == (
        0.16,
        1.09862,
        1.10312,
        config.magic,
    )
    assert monitor.activity[-1]["action"] == "ORDER"


def test_order_refused_when_the_advice_disagrees(cfg, broker, fake):
    monitor = primed(
        Monitor(live_cfg(cfg), broker, [SYMBOL], live_flag=True), features({"signal": 1, "raw_signal": 1})
    )
    assert "not advised" in monitor.execute(SYMBOL, -1)["message"]  # the page asked to sell into a buy
    primed(monitor, features())
    assert "not advised" in monitor.execute(SYMBOL, 1)["message"]  # the signal is gone
    assert monitor.execute("GBPUSD", 1)["message"] == "unknown symbol or side"
    assert fake.sent == []


def test_clean_handles_numpy_nan_and_datetimes():
    import numpy as np

    out = clean(
        {
            "a": np.float64("nan"),
            "b": np.int64(3),
            "c": pd.Timestamp("2026-01-02 03:04"),
            "d": (1.5, float("inf")),
        }
    )
    assert out == {"a": None, "b": 3, "c": "2026-01-02T03:04:00Z", "d": [1.5, None]}


# --- web server -----------------------------------------------------------------------------------


class StubMonitor:
    def __init__(self) -> None:
        self.orders: list[tuple[str, int]] = []

    def snapshot(self):
        return {"ready": True, "symbols": []}

    def chart(self, symbol):
        return {"symbol": symbol, "bars": []} if symbol == "EURUSD" else None

    def submit(self, symbol, side):
        self.orders.append((symbol, side))
        return {"ok": True, "message": "placed"}


@pytest.fixture
def server():
    monitor = StubMonitor()
    srv = DashboardServer(monitor, port=0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    base = {"Host": f"127.0.0.1:{srv.port}", "Content-Type": "application/json"}
    conn.request(method, path, body=payload, headers={**base, **(headers or {})})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data, response


def test_page_embeds_the_session_token_and_security_headers(server):
    status, data, response = call(server, "GET", "/")
    assert status == 200 and server.token.encode() in data
    assert "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")
    assert response.getheader("X-Content-Type-Options") == "nosniff"


def test_state_and_chart_endpoints(server):
    assert json.loads(call(server, "GET", "/api/state")[1]) == {"ready": True, "symbols": []}
    assert call(server, "GET", "/api/chart?symbol=EURUSD")[0] == 200
    assert call(server, "GET", "/api/chart?symbol=NOPE")[0] == 404
    assert call(server, "GET", "/nope")[0] == 404


def test_foreign_host_header_is_refused(server):
    """DNS rebinding: a malicious page re-pointing its own domain at 127.0.0.1."""
    status, _, _ = call(server, "GET", "/api/state", headers={"Host": f"attacker.example:{server.port}"})
    assert status == 403


def test_order_needs_the_token(server):
    status, data, _ = call(server, "POST", "/api/order", {"symbol": "EURUSD", "side": 1})
    assert status == 403 and b"token" in data
    status, _, _ = call(
        server, "POST", "/api/order", {"symbol": "EURUSD", "side": 1}, {TOKEN_HEADER: "guess"}
    )
    assert status == 403
    assert server.monitor.orders == []


def test_cross_origin_order_is_refused_even_with_the_token(server):
    headers = {TOKEN_HEADER: server.token, "Origin": "https://attacker.example"}
    status, _, _ = call(server, "POST", "/api/order", {"symbol": "EURUSD", "side": 1}, headers)
    assert status == 403 and server.monitor.orders == []


def test_valid_order_reaches_the_monitor(server):
    headers = {TOKEN_HEADER: server.token, "Origin": f"http://127.0.0.1:{server.port}"}
    status, data, _ = call(server, "POST", "/api/order", {"symbol": "EURUSD", "side": -1}, headers)
    assert status == 200 and json.loads(data)["ok"]
    assert server.monitor.orders == [("EURUSD", -1)]


def test_malformed_order_is_a_400(server):
    status, _, _ = call(server, "POST", "/api/order", {"symbol": "EURUSD"}, {TOKEN_HEADER: server.token})
    assert status == 400
