"""The decision engine, driven bar by bar through the simulated broker."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import at, bar, feed, make_sim, with_schedule

from tradebot.config import ManagementParams, RiskParams
from tradebot.engine import Engine
from tradebot.journal import Journal

SPREAD = 0.0001


def setup(config, eurusd, **risk_changes):
    if risk_changes:
        config = replace(config, risk=replace(config.risk, **risk_changes))
    sim = make_sim({"EURUSD": eurusd})
    engine = Engine(config, sim, execute=True)
    return config, sim, engine


def step(engine, sim, config, eurusd, row):
    now = feed(sim, "EURUSD", row)
    engine.on_bar("EURUSD", eurusd, config.for_symbol("EURUSD"), row, SPREAD, now)
    return now


def flat(minute, price=1.1000, **extra):
    return bar(at(minute), price, price + 0.0003, price - 0.0003, price, **extra)


def test_signal_opens_a_sized_position(config, eurusd):
    config, sim, engine = setup(config, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    (position,) = sim.positions(config.magic)
    assert position.side == 1
    assert position.price_open - position.sl == pytest.approx(config.risk.sl_atr * 0.0010)
    assert position.volume == 0.16  # $25 risk / $150 per lot (15-pip stop), rounded down
    assert engine.events[-1].kind == "open"


def test_each_bar_is_acted_on_once(config, eurusd):
    config, sim, engine = setup(
        config, eurusd, max_positions_per_symbol=5, max_total_positions=5, max_total_risk_percent=5
    )
    row = flat(0, signal=1, raw_signal=1)
    now = feed(sim, "EURUSD", row)
    for _ in range(3):  # live polling sees the same closed bar repeatedly
        engine.on_bar("EURUSD", eurusd, config.for_symbol("EURUSD"), row, SPREAD, now)
    assert len(sim.positions(config.magic)) == 1


def test_position_limits(config, eurusd):
    config, sim, engine = setup(config, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    step(engine, sim, config, eurusd, flat(15, signal=1, raw_signal=1))
    assert len(sim.positions(config.magic)) == 1
    assert engine.skips["positions"] == 1


@pytest.mark.parametrize(
    ("schedule", "minute", "expected"),
    [
        ({"session": "london"}, 0, True),  # 10:00 UTC
        ({"session": "asia"}, 0, False),
        ({"session": "custom", "start_hour_utc": 22, "end_hour_utc": 6}, 0, False),
        ({"session": "all", "trade_weekends": False}, 5 * 24 * 60, False),  # Saturday
    ],
)
def test_session_and_weekend_guard(config, eurusd, schedule, minute, expected):
    config = with_schedule(config, **schedule)
    config, sim, engine = setup(config, eurusd)
    step(engine, sim, config, eurusd, flat(minute, signal=1, raw_signal=1))
    assert bool(sim.positions(config.magic)) is expected


def test_news_blackout(config, eurusd, tmp_path):
    news = tmp_path / "news.json"
    event_time = at(15 + 20)  # 20 minutes after the decision time
    news.write_text(
        json.dumps(
            [{"time_utc": event_time.isoformat() + "Z", "currency": "USD", "impact": "high", "event": "CPI"}]
        )
    )
    config = with_schedule(config, news_file=str(news), news_before_minutes=30)
    config, sim, engine = setup(config, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    assert not sim.positions(config.magic)
    assert "news" in engine.events[-1].detail


def test_kill_switch_only_in_live_mode(config, eurusd, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "STOP").write_text("")
    config, sim, engine = setup(config, eurusd)
    engine.live = True
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    assert not sim.positions(config.magic) and "kill switch" in engine.events[-1].detail


def test_daily_loss_limit_pauses_entries(config, eurusd):
    config, sim, engine = setup(config, eurusd, daily_loss_limit_percent=0.2, max_consecutive_losses=0)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    step(engine, sim, config, eurusd, bar(at(15), 1.1000, 1.1000, 1.0950, 1.0960))  # stopped out: -$25
    step(engine, sim, config, eurusd, flat(30, 1.0960, signal=1, raw_signal=1))
    assert not sim.positions(config.magic)
    assert engine.skips["daily loss limit"] == 1


def test_losing_streak_pauses_until_next_day(config, eurusd):
    config, sim, engine = setup(config, eurusd, max_consecutive_losses=2, daily_loss_limit_percent=0)
    minute = 0
    for _ in range(2):
        step(engine, sim, config, eurusd, flat(minute, signal=1, raw_signal=1))
        step(engine, sim, config, eurusd, bar(at(minute + 15), 1.1, 1.1, 1.0950, 1.0960))
        minute += 30
    step(engine, sim, config, eurusd, flat(minute, signal=1, raw_signal=1))
    assert engine.skips["losing streak"] == 1
    step(engine, sim, config, eurusd, flat(minute + 24 * 60, signal=1, raw_signal=1))  # next day
    assert len(sim.positions(config.magic)) == 1


def test_wide_spread_blocks_entry(config, eurusd):
    config, sim, engine = setup(config, eurusd, max_spread_atr=0.05)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))  # spread 0.1 ATR
    assert engine.skips["spread"] == 1


def test_breakeven_then_trailing_only_tighten(config, eurusd):
    config = replace(
        config,
        management=ManagementParams(partial_tp=False, breakeven_r=1.0, trail_start_r=2.0, trail_atr=1.0),
    )
    config, sim, engine = setup(config, eurusd, tp_atr=20)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    position = sim.positions(config.magic)[0]
    entry, risk = position.price_open, position.initial_risk
    step(engine, sim, config, eurusd, flat(15, entry + 1.2 * risk))
    assert position.sl == pytest.approx(entry + 0.1 * risk, abs=1e-5)  # breakeven + buffer
    step(engine, sim, config, eurusd, flat(30, entry + 3.0 * risk))
    trailed = position.sl
    assert trailed == pytest.approx(entry + 3.0 * risk - 0.0010, abs=1e-5)
    step(engine, sim, config, eurusd, flat(45, entry + 2.6 * risk))  # pullback: stop must not loosen
    assert position.sl == trailed


def test_partial_profit_is_taken_once(config, eurusd):
    config = replace(
        config,
        management=ManagementParams(partial_tp=True, partial_tp_r=1.5, breakeven_r=99, trail_start_r=99),
    )
    config, sim, engine = setup(config, eurusd, tp_atr=20)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    position = sim.positions(config.magic)[0]
    for i in range(1, 5):
        step(engine, sim, config, eurusd, flat(15 * i, position.price_open + 2 * position.initial_risk))
    assert position.volume == pytest.approx(0.08)
    assert sum(e.kind == "partial" for e in engine.events) == 1


def test_opposite_signal_closes_position(config, eurusd):
    config, sim, engine = setup(config, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    step(engine, sim, config, eurusd, flat(15, signal=-1, raw_signal=-1))
    (position,) = sim.positions(config.magic)
    assert position.side == -1  # closed the buy, then took the sell
    assert sim.trades[0].exit_reason == "opposite signal"


def test_dry_run_sends_nothing_but_journals(config, eurusd, tmp_path):
    sim = make_sim({"EURUSD": eurusd})
    journal = Journal(tmp_path / "j.sqlite")
    engine = Engine(config, sim, execute=False, journal=journal)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    assert not sim.positions(config.magic)
    rows = journal.summary(1, at(60))
    assert rows == [("EURUSD", "DRY_OPEN", 1, 0.0)]
    journal.close()


def test_restart_does_not_reenter_the_same_bar(config, eurusd):
    config, sim, engine = setup(
        config, eurusd, max_positions_per_symbol=3, max_total_positions=3, max_total_risk_percent=3
    )
    row = flat(0, signal=1, raw_signal=1)
    now = feed(sim, "EURUSD", row)
    engine.on_bar("EURUSD", eurusd, config.for_symbol("EURUSD"), row, SPREAD, now)
    restarted = Engine(config, sim, execute=True, live=True)  # fresh memory, same broker history
    restarted.on_bar("EURUSD", eurusd, config.for_symbol("EURUSD"), row, SPREAD, now + timedelta(seconds=30))
    assert len(sim.positions(config.magic)) == 1
    assert "duplicate" in restarted.events[-1].detail


def test_portfolio_risk_cap(config, eurusd):
    config = replace(
        config,
        risk=RiskParams(
            risk_percent=0.5, max_total_risk_percent=0.5, max_positions_per_symbol=3, max_total_positions=3
        ),
    )
    config, sim, engine = setup(config, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    step(engine, sim, config, eurusd, flat(15, signal=1, raw_signal=1))
    assert len(sim.positions(config.magic)) == 1
    assert engine.skips["portfolio risk"] == 1
