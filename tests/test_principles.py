"""Trading principles as rules: the drawdown limit, the written plan, and the review in R."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import FakeMT5, at, bar, feed, make_sim
from test_advisor_ui import SYMBOL, call
from test_engine import SPREAD, flat, step

from tradebot import cli
from tradebot.broker.base import Deal
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import (
    STRATEGIES,
    BotConfig,
    ConfigError,
    MT5Login,
    RiskParams,
    ScheduleParams,
    load_config,
)
from tradebot.data import synthetic_bars
from tradebot.engine import Engine
from tradebot.journal import Journal
from tradebot.plan import Section, plan_title, to_markdown, trading_plan
from tradebot.review import ReviewedTrade, closed_trades, summarize, verdict
from tradebot.risk import drawdown_percent
from tradebot.strategy import FILTERS
from tradebot.ui.monitor import Monitor
from tradebot.ui.server import DashboardServer

NOW = datetime(2026, 3, 4, 12, 7)
LOSS = (1.1000, 1.1000, 1.0950, 1.0960)  # a bar that runs through a 15-pip stop


# --- the drawdown limit ---------------------------------------------------------------------------


def deal(minute: int, profit: float) -> Deal:
    return Deal(minute, minute, "EURUSD", at(minute), 1, 0.1, 1.1, profit, "out")


def test_drawdown_is_measured_from_the_highest_balance():
    # 10,000 -> 10,100 -> 9,800: 300 below a peak of 10,100
    assert drawdown_percent([deal(0, 100.0), deal(15, -300.0)], 9_800.0) == pytest.approx(300 / 10_100 * 100)
    assert drawdown_percent([deal(15, -300.0), deal(0, 100.0)], 9_800.0) == pytest.approx(2.9703, abs=1e-4)
    assert drawdown_percent([deal(0, -50.0), deal(15, 80.0)], 10_030.0) == 0.0  # at a new high
    assert drawdown_percent([], 10_000.0) == 0.0


def engine_with(config, eurusd, live: bool, **risk):
    risk = {
        "daily_loss_limit_percent": 0,
        "weekly_loss_limit_percent": 0,
        "max_consecutive_losses": 0,
        **risk,
    }
    config = replace(config, risk=replace(config.risk, **risk))
    sim = make_sim({"EURUSD": eurusd})
    return config, sim, Engine(config, sim, execute=True, live=live)


def lose_once(engine, sim, config, eurusd, minute: int = 0) -> None:
    step(engine, sim, config, eurusd, flat(minute, signal=1, raw_signal=1))
    step(engine, sim, config, eurusd, bar(at(minute + 15), *LOSS))  # stopped out: about -$25, 0.25%


def test_drawdown_limit_pauses_live_entries(config, eurusd):
    config, sim, engine = engine_with(config, eurusd, live=True, max_drawdown_percent=0.2)
    lose_once(engine, sim, config, eurusd)
    step(engine, sim, config, eurusd, flat(30, 1.0960, signal=1, raw_signal=1))
    assert not sim.positions(config.magic)
    assert engine.skips["drawdown limit"] == 1
    assert "below the 365-day peak (limit 0.2%), entries paused" in engine.events[-1].detail
    step(
        engine, sim, config, eurusd, flat(30 + 24 * 60, 1.0960, signal=1, raw_signal=1)
    )  # still paused next day
    assert not sim.positions(config.magic) and engine.skips["drawdown limit"] == 2


def test_a_backtest_trades_through_the_drawdown(config, eurusd):
    """Research runs must show what the rules do; the circuit breaker would only cut the test short."""
    config, sim, engine = engine_with(config, eurusd, live=False, max_drawdown_percent=0.2)
    lose_once(engine, sim, config, eurusd)
    step(engine, sim, config, eurusd, flat(30, 1.0960, signal=1, raw_signal=1))
    assert len(sim.positions(config.magic)) == 1 and "drawdown limit" not in engine.skips


def test_drawdown_window_and_off_switch(config, eurusd):
    three_days = 3 * 24 * 60
    config, sim, engine = engine_with(config, eurusd, live=True, max_drawdown_percent=0.2, drawdown_days=2)
    lose_once(engine, sim, config, eurusd)
    step(engine, sim, config, eurusd, flat(three_days, 1.0960, signal=1, raw_signal=1))
    assert len(sim.positions(config.magic)) == 1  # the loss is older than the two-day window

    config, sim, engine = engine_with(config, eurusd, live=True, max_drawdown_percent=0)
    lose_once(engine, sim, config, eurusd)
    step(engine, sim, config, eurusd, flat(30, 1.0960, signal=1, raw_signal=1))
    assert len(sim.positions(config.magic)) == 1


def test_risk_status_reports_every_limit(config, eurusd):
    config, sim, engine = engine_with(config, eurusd, live=True, max_drawdown_percent=5.0)
    lose_once(engine, sim, config, eurusd)
    now = step(engine, sim, config, eurusd, flat(30, 1.0960, signal=1, raw_signal=1))
    status = engine.risk_status(now)
    assert status.day_pnl == pytest.approx(-24.0) and status.day_percent == pytest.approx(-0.24)
    assert status.week_pnl == status.day_pnl
    assert status.drawdown == pytest.approx(0.24) and status.drawdown_limit == 5.0
    assert (status.losing_streak, status.entries_today) == (1, 2)
    assert (status.positions, status.positions_limit) == (1, config.risk.max_total_positions)
    assert status.open_risk_percent == pytest.approx(0.24, abs=0.02)
    assert status.open_risk_limit == config.risk.max_total_risk_percent
    assert status.blocked == ""
    # the dashboard's sentence is the engine's own reason
    tight = replace(config, risk=replace(config.risk, max_drawdown_percent=0.2))
    assert (
        Engine(tight, sim, execute=False, live=True)
        .risk_status(now)
        .blocked.startswith("drawdown limit: 0.2%")
    )


def test_live_limits_read_old_deals_once_a_day(config, eurusd):
    config, sim, engine = engine_with(config, eurusd, live=True)
    calls = []
    real = sim.deals
    sim.deals = lambda since, magic: calls.append(since) or real(since, magic)
    now = feed(sim, "EURUSD", flat(0))
    for _ in range(3):
        engine.risk_status(now)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert calls == [day - timedelta(days=365), day, day, day]  # the long history once, then only today's


def test_config_validates_the_drawdown_settings():
    with pytest.raises(ConfigError, match="max_drawdown_percent"):
        BotConfig(risk=RiskParams(max_drawdown_percent=-1)).validate()
    with pytest.raises(ConfigError, match="drawdown_days"):
        BotConfig(risk=RiskParams(drawdown_days=0)).validate()
    shipped = load_config(cli.Path(__file__).resolve().parent.parent / "config" / "config.json", env={})
    assert (shipped.risk.max_drawdown_percent, shipped.risk.drawdown_days) == (10.0, 365)


def test_backtest_says_when_the_live_limit_would_have_tripped():
    days = pd.date_range("2026-01-01", periods=6, freq="D")
    falling = pd.Series([10_000, 10_100, 9_900, 9_500, 9_050, 9_200], index=days, dtype=float)
    assert cli._drawdown_trip(falling, 10.0, 365) == days[4]  # 9,050 is 10.4% below 10,100
    assert cli._drawdown_trip(falling, 20.0, 365) is None
    assert cli._drawdown_trip(falling, 0.0, 365) is None
    slow = pd.Series([10_000, 9_600, 9_200, 8_900], index=pd.date_range("2026-01-01", periods=4, freq="60D"))
    assert cli._drawdown_trip(slow, 10.0, 90) is None  # each 90-day window lost less than 10%
    assert cli._drawdown_trip(slow, 10.0, 365) == slow.index[3]


def test_journal_records_the_stop_and_target(config, eurusd, tmp_path):
    sim = make_sim({"EURUSD": eurusd})
    journal = Journal(tmp_path / "j.sqlite")
    engine = Engine(config, sim, execute=True, journal=journal)
    now = feed(sim, "EURUSD", flat(0, signal=1, raw_signal=1))
    engine.on_bar("EURUSD", eurusd, config.for_symbol("EURUSD"), flat(0, signal=1, raw_signal=1), SPREAD, now)
    kind, price, sl, tp = journal._db.execute("SELECT kind, price, sl, tp FROM events").fetchone()
    assert kind == "OPEN"
    assert sl == pytest.approx(price - 0.0015) and tp == pytest.approx(price + 0.0030)
    journal.close()


# --- the written plan -----------------------------------------------------------------------------


def lines_of(sections: list[Section], title: str) -> str:
    return "\n".join(next(s for s in sections if s.title == title).lines)


def test_plan_covers_what_a_trading_plan_must_state():
    sections = trading_plan(BotConfig(symbols=("EURUSD", "XAUUSD"), timeframe="H1"))
    assert [s.title for s in sections] == [
        "What is traded, and when", "Entry", "Exit", "Position size", "Limits that stop new entries",
        "When not to trade", "Review", "What these rules have measured",
    ]  # fmt: skip
    assert "EURUSD, XAUUSD" in lines_of(sections, "What is traded, and when")
    assert "07:00 to 21:00 UTC, Monday to Friday" in lines_of(sections, "What is traded, and when")
    exits = lines_of(sections, "Exit")
    assert "Stop: 1.5 ATR" in exits and "reward is 2.0 times the risk" in exits and "never widened" in exits
    assert "risks 0.25% of equity" in lines_of(sections, "Position size")
    limits = lines_of(sections, "Limits that stop new entries")
    for expected in (
        "at most 1% of equity",
        "loss of 2%",
        "loss of 5%",
        "Drawdown: 10% below",
        "4 losses in a row",
    ):
        assert expected in limits
    avoid = lines_of(sections, "When not to trade")
    assert "wider than 0.25 ATR" in avoid and "file named STOP" in avoid and "news_blackouts.json" in avoid


@pytest.mark.parametrize("name", STRATEGIES)
def test_plan_states_each_setup_and_only_its_own_filters(name):
    config = BotConfig(timeframe="M15").with_strategy(name=name, trend_timeframes="auto")
    entry = lines_of(trading_plan(config), "Entry")
    assert entry.startswith("Setup (")
    assert ("last completed H1 and H4 bars" in entry) is ("trend" in FILTERS[name])
    assert ("MACD" in entry) is ("macd" in FILTERS[name])
    assert ("Room to run" in entry) is ("sr" in FILTERS[name])
    assert ("Not overextended" in entry) is ("rsi" in FILTERS[name])
    assert plan_title(config).endswith("on M15")
    hourly = lines_of(trading_plan(replace(config, timeframe="H1")), "Entry")
    assert name == "pullback" or "an H1 bar" in hourly or "An H1 bar" in hourly
    assert "A H1" not in hourly and "h1" not in hourly


def test_plan_follows_the_settings():
    base = BotConfig()
    breakout = lines_of(trading_plan(base.with_strategy(name="breakout", breakout_min_atr=0.4)), "Entry")
    assert "closes at least 0.4 ATR beyond" in breakout
    off = replace(base, risk=replace(base.risk, max_drawdown_percent=0, daily_loss_limit_percent=0))
    limits = lines_of(trading_plan(off), "Limits that stop new entries")
    assert "Drawdown" not in limits and "Day:" not in limits and "Week:" in limits
    fixed = replace(base, risk=replace(base.risk, fixed_lot=0.1))
    assert lines_of(trading_plan(fixed), "Position size") == "Every trade is 0.1 lots (fixed)."
    overridden = replace(
        base, overrides={"EURUSD": {"risk": {"risk_percent": 0.1}, "backtest": {"min_spread_points": 5}}}
    )
    what = lines_of(trading_plan(overridden), "What is traded, and when")
    assert "EURUSD: risk.risk_percent = 0.1" in what and "min_spread_points" not in what


def test_plan_is_honest_about_the_edge():
    config = BotConfig()
    record = {"start": "2016-08-29T00:00:00Z", "end": datetime(2026, 11, 11), "trades": 663, "avg_r": -0.18,
              "win_rate": 37.1, "profit_factor": 0.72, "return_pct": -26.07}  # fmt: skip
    edge = lines_of(trading_plan(config, record), "What these rules have measured")
    assert "(2016-08 to 2026-11, 663 trades) these rules averaged -0.18R per trade" in edge
    assert "negative expectancy" in edge and "Orders: off. This is a dry run" in edge
    assert "Real-money accounts are refused" in edge
    winning = lines_of(
        trading_plan(config, {**record, "avg_r": 0.12, "profit_factor": None}),
        "What these rules have measured",
    )
    assert "in-sample" in winning and "profit factor n/a" in winning and "negative expectancy" not in winning
    unmeasured = lines_of(trading_plan(config), "What these rules have measured")
    assert "Not measured here" in unmeasured and "keep this plan in dry run" in unmeasured
    live = replace(config, dry_run=False, live_trading_confirm=True)
    assert "Orders: enabled in the config" in lines_of(trading_plan(live), "What these rules have measured")


def test_plan_as_markdown():
    config = BotConfig()
    text = to_markdown(config, trading_plan(config))
    assert text.startswith("# Trading plan: EMA cross on M15\n")
    assert "\n## Entry\n\n- Setup (EMA cross): EMA 10 closes across EMA 50" in text


@pytest.fixture
def workspace(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    synthetic_bars(n=5000, seed=5).to_csv(data / "EURUSD_M15.csv", index=False)
    root = cli.Path(__file__).resolve().parent.parent
    raw = json.loads((root / "config" / "config.json").read_text())
    raw.update(
        symbols=["EURUSD"], timeframe="M15", overrides={}, schedule={"session": "all", "news_file": ""}
    )
    (tmp_path / "config.json").write_text(json.dumps(raw))
    return tmp_path


def test_plan_command_measures_the_rules_and_writes_the_file(workspace, capsys):
    out = workspace / "PLAN.md"
    args = [
        "-c",
        str(workspace / "config.json"),
        "plan",
        "--strategy",
        "breakout",
        "--data",
        str(workspace / "data"),
    ]
    assert cli.main([*args, "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Trading plan: Breakout on M15") and "trades) these rules averaged" in text
    assert "Limits that stop new entries" in capsys.readouterr().out
    assert cli.main([*args, "--no-backtest", "--out", str(out)]) == 0
    assert "Not measured here" in out.read_text(encoding="utf-8")
    empty = workspace / "nothing"
    assert (
        cli.main(["-c", str(workspace / "config.json"), "plan", "--data", str(empty), "--out", str(out)]) == 0
    )
    assert "Not measured here" in out.read_text(encoding="utf-8")  # no history is not an error


# --- the review -----------------------------------------------------------------------------------


def fake_deal(fake, ticket, position, when, entry, profit, price=1.1, kind=0, comment="", magic=7):
    return SimpleNamespace(ticket=ticket, position_id=position, symbol=SYMBOL, time=fake.server_epoch(when), type=kind,
                           volume=0.10, price=price, profit=profit, commission=0.0, swap=0.0, fee=0.0, magic=magic,
                           entry=entry, comment=comment)  # fmt: skip


@pytest.fixture
def traded() -> MT5Broker:
    """Three closed trades with a 15-pip stop ($15 at 0.10 lots), one still open, one foreign."""
    fake = FakeMT5(now=NOW)
    day = datetime(2026, 3, 3)
    fake.deals = [
        fake_deal(fake, 1, 11, day.replace(hour=9), 0, 0.0, comment="tradebot"),
        fake_deal(fake, 2, 11, day.replace(hour=11), 1, 30.0, kind=1, comment="[tp 1.10300]"),
        fake_deal(fake, 3, 12, day.replace(hour=12), 0, 0.0, comment="tradebot"),
        fake_deal(fake, 4, 12, day.replace(hour=13), 1, -15.0, kind=1, comment="[sl 1.09850]"),
        fake_deal(fake, 5, 13, day.replace(hour=14), 0, 0.0, comment="tradebot manual"),
        fake_deal(fake, 6, 13, day.replace(hour=15), 1, -7.5, kind=1, comment="closed by hand"),
        fake_deal(fake, 7, 14, day.replace(hour=16), 0, 0.0, comment="tradebot"),  # still open
        fake_deal(fake, 8, 15, day.replace(hour=16), 1, 99.0, magic=999),  # another program's trade
    ]
    fake.orders = [
        SimpleNamespace(position_id=p, time_setup=1, volume_initial=0.10, sl=1.0985) for p in (11, 12, 13, 14)
    ]
    fake.positions = [SimpleNamespace(ticket=14, magic=7, symbol=SYMBOL, type=0, volume=0.10, price_open=1.1, sl=1.0985,
                                      tp=1.103, time=fake.server_epoch(day.replace(hour=16)), profit=1.0, comment="tradebot")]  # fmt: skip
    return MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)


def test_closed_trades_are_measured_in_r(traded):
    trades = closed_trades(traded, 7, NOW - timedelta(days=30))
    assert [t.ticket for t in trades] == [11, 12, 13]  # open and foreign positions are left out
    assert [t.r for t in trades] == pytest.approx([2.0, -1.0, -0.5])
    assert [t.exit for t in trades] == ["target", "stop", "other"]
    assert [t.manual for t in trades] == [False, False, True]
    assert trades[0].opened == datetime(2026, 3, 3, 9) and trades[0].closed == datetime(2026, 3, 3, 11)


def test_summary_and_verdict(traded):
    trades = closed_trades(traded, 7, NOW - timedelta(days=30))
    s = summarize(trades)
    assert (s.trades, s.wins, s.stops, s.targets, s.longest_losing_streak) == (3, 1, 1, 1, 2)
    assert s.net == pytest.approx(7.5) and s.profit_factor == pytest.approx(30 / 22.5)
    assert s.avg_r == pytest.approx(0.5 / 3) and s.win_rate == pytest.approx(100 / 3)
    assert "+0.17R per trade over 3 trades. That is too few to judge the rules" in verdict(s)
    assert verdict(summarize([])) == "No closed trades in this period."

    def many(r: float) -> list[ReviewedTrade]:
        return [ReviewedTrade(i, SYMBOL, 1, NOW, NOW, 10 * r, 10.0, False, "stop") for i in range(60)]

    assert "the rules are losing. Do not raise the size" in verdict(summarize(many(-0.2)))
    assert "positive so far" in verdict(summarize(many(0.3)))
    unmeasured = summarize([ReviewedTrade(1, SYMBOL, 1, NOW, NOW, 5.0, None, False, "other")])
    assert unmeasured.avg_r is None and "cannot be measured in R" in verdict(unmeasured)


def test_review_command_separates_the_bots_signals_from_your_own_calls(traded, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"symbols": ["EURUSD"], "magic": 7}))
    journal = Journal(tmp_path / "trade_journal.sqlite")
    engine = Engine(BotConfig(symbols=("EURUSD",)), traded, execute=False, journal=journal)
    engine._journal(NOW - timedelta(days=1), "DRY_OPEN", "EURUSD", 1, 0.1, 1.1, None, "test")
    journal.close()
    monkeypatch.setattr(cli, "_connect", lambda config: (traded, traded.account(), [SYMBOL]))
    monkeypatch.setattr(traded, "shutdown", lambda: None)
    monkeypatch.setattr(cli.console, "width", 200)
    assert cli.main(["-c", "config.json", "review", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "3 closed trades" in out and "33.3% winners" in out and "too few to judge" in out
    bot = next(line for line in out.splitlines() if line.startswith("The bot's signals"))
    mine = next(line for line in out.splitlines() if line.startswith("Your own calls"))
    assert bot.split()[3:] == ["2", "50.0", "+15.00", "2.00", "+0.50", "1", "1", "1"]
    assert mine.split()[3:] == ["1", "0.0", "-7.50", "0.00", "-0.50", "0", "0", "1"]
    assert "logged 1 signal in this period without sending an order" in out
    assert "tradebot backtest --from 2026-02-02" in out


# --- the dashboard --------------------------------------------------------------------------------


@pytest.fixture
def monitor() -> Monitor:
    fake = FakeMT5(now=NOW)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    config = BotConfig(
        symbols=(SYMBOL,),
        timeframe="M15",
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )
    return Monitor(config, broker, [SYMBOL])


def test_dashboard_state_carries_the_risk_limits(monitor):
    monitor.refresh()
    risk = monitor.snapshot()["risk"]
    json.dumps(risk, allow_nan=False)
    assert risk["daily_limit"] == 2.0 and risk["weekly_limit"] == 5.0 and risk["drawdown_limit"] == 10.0
    assert (risk["drawdown_days"], risk["streak_limit"], risk["entries_limit"]) == (365, 4, 8)
    assert (risk["day_pnl"], risk["drawdown"], risk["positions"], risk["blocked"]) == (0.0, 0.0, 0, "")


def test_plan_endpoint_follows_the_setup_on_screen(monitor):
    server = DashboardServer(monitor, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        status, data, _ = call(server, "GET", "/api/plan")
        plan = json.loads(data)
        assert status == 200 and plan["title"] == "Trading plan: EMA cross on M15"
        assert [s["title"] for s in plan["sections"]][1] == "Entry"
        assert monitor._apply_strategy("retest")["ok"]
        assert (
            json.loads(call(server, "GET", "/api/plan")[1])["title"]
            == "Trading plan: Break and retest on M15"
        )
        monitor.track_record = {"start": "2024-01-01T00:00:00Z", "end": "2026-11-01T00:00:00Z", "trades": 40,
                                "avg_r": -0.1, "win_rate": 39.0, "profit_factor": None, "return_pct": -3.0}  # fmt: skip
        measured = json.loads(call(server, "GET", "/api/plan")[1])["sections"][-1]["lines"][0]
        assert "(2024-01 to 2026-11, 40 trades) these rules averaged -0.10R" in measured
        monitor.track_record, monitor._track_pending = {}, True  # a switch restarts the backtest
        waiting = json.loads(call(server, "GET", "/api/plan")[1])["sections"][-1]["lines"][0]
        assert waiting.startswith("Being measured")
        page = call(server, "GET", "/")[1].decode()
        assert 'id="plan-dialog"' in page and 'id="limits"' in page
    finally:
        server.shutdown()
        server.server_close()
