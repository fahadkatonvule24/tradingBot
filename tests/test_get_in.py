"""Orders you take from the dashboard on your own call: "Get in" on a bot signal its limits held
back, and "Duplicate" of the bot's open trade. Both are logged as manual."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_advisor_ui import NOW, SYMBOL, features, live_cfg, primed
from test_advisor_ui import broker as broker
from test_advisor_ui import cfg as cfg
from test_advisor_ui import fake as fake
from test_principles import traded as traded

from tradebot.book import Book
from tradebot.config import BotConfig
from tradebot.ui.monitor import OWN_SIZE_MAX_RISK, Monitor, get_in_rule

SIGNAL = {"signal": 1, "raw_signal": 1}


def outside_hours(cfg: BotConfig) -> BotConfig:
    return replace(live_cfg(cfg), schedule=replace(cfg.schedule, session="asia"))  # 23-08 UTC; it is 12:07


def test_get_in_takes_a_signal_the_limits_skipped_as_your_own_call(cfg, broker, fake):
    config = outside_hours(cfg)
    monitor = primed(Monitor(config, broker, [SYMBOL], live_flag=True), features(SIGNAL))
    monitor.refresh()
    row = monitor.snapshot()["symbols"][0]
    assert row["advice"]["action"] == "SKIP"
    offer = row["get_in"]
    assert offer["side"] == 1 and offer["over"].startswith("session") and offer["plan"]["volume"] == 0.16
    assert "blocked by a risk rule: session" in monitor.execute(SYMBOL, 1, manual=True)["message"]
    assert fake.sent == []
    result = monitor.execute(SYMBOL, 1, manual=True, override=True)
    assert result["ok"], result
    (order,) = fake.sent
    assert (order["volume"], order["comment"], order["magic"]) == (0.16, "tradebot manual", config.magic)
    assert "over the bot's limit (session" in result["message"]
    assert monitor.activity[-1]["action"] == "ORDER"


def test_get_in_sets_aside_the_position_and_open_risk_limits_not_the_size(cfg, broker, fake):
    fake.positions = [SimpleNamespace(ticket=5, magic=0, symbol=SYMBOL, type=0, volume=0.30, price_open=1.0990,
                                      sl=1.0950, tp=0.0, time=fake.server_epoch(NOW - timedelta(hours=1)),
                                      profit=0.0, comment="")]  # fmt: skip
    monitor = primed(Monitor(live_cfg(cfg), broker, [SYMBOL], live_flag=True), features(SIGNAL))
    held = monitor.advice_now(SYMBOL)
    assert held.action == "SKIP" and held.blocker.startswith("positions: 1 open")
    result = monitor.execute(SYMBOL, 1, manual=True, override=True)
    assert result["ok"] and fake.sent[-1]["volume"] == 0.16  # the configured risk per trade, no more

    tiny = replace(live_cfg(cfg), risk=replace(cfg.risk, risk_percent=0.01))
    unsizable = primed(Monitor(tiny, broker, [SYMBOL], live_flag=True), features(SIGNAL))
    assert "cannot size it" in unsizable.execute(SYMBOL, 1, manual=True, override=True)["message"]


def test_get_in_needs_a_held_back_bot_entry_and_never_trades_through_bad_data(cfg, broker, fake):
    monitor = primed(Monitor(outside_hours(cfg), broker, [SYMBOL], live_flag=True), features())
    assert "nothing to get in on" in monitor.execute(SYMBOL, 1, manual=True, override=True)["message"]
    assert "your own call" in monitor.execute(SYMBOL, 1, override=True)["message"]
    assert fake.sent == []
    stale = SimpleNamespace(
        action="SKIP", side=1, blocker="data: quote is stale", plan=SimpleNamespace(rejected="")
    )
    assert get_in_rule(stale) is None
    assert get_in_rule(SimpleNamespace(action="WAIT", side=1, blocker="session: closed", plan=None)) is None
    join = SimpleNamespace(
        action="JOIN", side=-1, blocker="daily loss limit: -40", plan=SimpleNamespace(rejected="")
    )
    assert get_in_rule(join) == "daily loss limit: -40"


def test_your_size_sends_your_lots_where_the_smallest_lot_is_over_the_limit(cfg, broker, fake):
    """Gold in a small account: the smallest lot risks more than the size per trade, so the bot has
    no size. Your size sends the lots you typed, with the bot's stop and target for that side."""
    tiny = replace(live_cfg(cfg), risk=replace(cfg.risk, risk_percent=0.01))
    monitor = primed(Monitor(tiny, broker, [SYMBOL], live_flag=True), features())
    plan = monitor.advice_now(SYMBOL).plans["sell"]
    assert plan.volume == 0 and plan.rejected.startswith("minimum lot would risk")
    result = monitor.execute(SYMBOL, -1, manual=True, volume=0.02)
    assert result["ok"], result
    (order,) = fake.sent
    assert order["type"] == fake.ORDER_TYPE_SELL
    assert (order["volume"], order["comment"], order["magic"]) == (0.02, "tradebot manual", tiny.magic)
    assert order["sl"] == pytest.approx(plan.stop, abs=1e-4) and order["tp"] == pytest.approx(
        plan.target, abs=1e-4
    )
    assert "your size" in result["message"] and "over the bot's 0.01%" in result["message"]
    assert monitor.activity[-1]["action"] == "ORDER"


def test_your_size_keeps_the_guards_the_lot_limits_and_a_ceiling(cfg, broker, fake):
    monitor = primed(Monitor(live_cfg(cfg), broker, [SYMBOL], live_flag=True), features())
    assert "manual order" in monitor.execute(SYMBOL, 1, volume=0.01)["message"]
    assert "without Get in" in monitor.execute(SYMBOL, 1, manual=True, override=True, volume=0.01)["message"]
    assert (
        "outside the broker's 0.01 to 100" in monitor.execute(SYMBOL, 1, manual=True, volume=0.001)["message"]
    )
    assert (
        "not a multiple of the lot step" in monitor.execute(SYMBOL, 1, manual=True, volume=0.015)["message"]
    )
    assert (
        f"more than the {OWN_SIZE_MAX_RISK:g}%"
        in monitor.execute(SYMBOL, 1, manual=True, volume=50.0)["message"]
    )
    closed = primed(Monitor(outside_hours(cfg), broker, [SYMBOL], live_flag=True), features())
    assert "blocked by a risk rule: session" in closed.execute(SYMBOL, 1, manual=True, volume=0.01)["message"]
    dry = primed(Monitor(cfg, broker, [SYMBOL], live_flag=True), features())
    assert "dry_run" in dry.execute(SYMBOL, 1, manual=True, volume=0.01)["message"]
    assert fake.sent == []
    assert monitor.execute(SYMBOL, 1, manual=True, volume=0.01)["ok"] and fake.sent[-1]["volume"] == 0.01


def test_the_symbol_row_carries_the_lot_limits_for_your_size(cfg, broker):
    monitor = primed(Monitor(cfg, broker, [SYMBOL]), features())
    monitor.refresh()
    row = monitor.snapshot()["symbols"][0]
    assert (row["volume_min"], row["volume_step"]) == (0.01, 0.01)
    assert row["auto_trade"] is True
    quiet = replace(cfg, overrides={SYMBOL: {"risk": {"auto_trade": False}}})
    signals_only = primed(Monitor(quiet, broker, [SYMBOL]), features())
    signals_only.refresh()
    assert signals_only.snapshot()["symbols"][0]["auto_trade"] is False


def bot_trade(fake, config: BotConfig, sl: float = 1.0960, tp: float = 1.1050, magic: int | None = None):
    opened = fake.server_epoch(NOW - timedelta(hours=1))
    fake.positions = [SimpleNamespace(ticket=77, magic=config.magic if magic is None else magic, symbol=SYMBOL,
                                      type=0, volume=0.10, price_open=1.0990, sl=sl, tp=tp, time=opened,
                                      profit=0.0, comment="tradebot")]  # fmt: skip
    fake.orders = [SimpleNamespace(position_id=77, time_setup=0, volume_initial=0.10, sl=sl)]


def test_duplicate_copies_the_bots_trade_as_your_own_call(cfg, broker, fake):
    config = live_cfg(cfg)
    bot_trade(fake, config)
    monitor = primed(Monitor(config, broker, [SYMBOL], live_flag=True), features())
    monitor.refresh()
    offer = monitor.snapshot()["positions"][0]["duplicate"]
    assert (offer["volume"], offer["stop"], offer["target"]) == (0.1, 1.096, 1.105)
    assert offer["risk_amount"] == pytest.approx(41.2, abs=0.5)  # from the ask 1.10012 to 1.09600, 0.1 lot
    result = monitor.duplicate(77)
    assert result["ok"] and "duplicate of #77" in result["message"]
    (order,) = fake.sent
    assert order["type"] == fake.ORDER_TYPE_BUY
    assert (order["volume"], order["sl"], order["tp"], order["comment"], order["magic"]) == (
        0.1, 1.096, 1.105, "tradebot manual", config.magic,
    )  # fmt: skip


def test_duplicate_refuses_what_it_cannot_copy(cfg, broker, fake):
    config = live_cfg(cfg)
    monitor = primed(Monitor(config, broker, [SYMBOL], live_flag=True), features())
    assert "not open any more" in monitor.duplicate(77)["message"]
    bot_trade(fake, config, magic=0)  # placed by hand on the phone
    (p,) = broker.positions(None)
    assert monitor.duplicate_plan(p) == {"refused": "only the bot's trades are duplicated here"}
    bot_trade(fake, config, sl=1.10001)  # price is through the stop already
    (p,) = broker.positions(None)
    assert "too close to the trade's stop" in monitor.duplicate_plan(p)["refused"]
    bot_trade(fake, config)
    dry = primed(Monitor(cfg, broker, [SYMBOL], live_flag=True), features())
    assert "dry_run" in dry.duplicate(77)["message"]
    dry.refresh()
    assert dry.snapshot()["positions"][0]["duplicate"] is None  # no button while orders are off
    assert fake.sent == []


def test_the_bot_scorecard_leaves_out_your_own_calls(traded, tmp_path):
    """Get in and Duplicate carry the bot's number: "manual" in the comment keeps them out."""
    config = BotConfig(symbols=(SYMBOL,), magic=7)
    monitor = Monitor(config, traded, [SYMBOL], book=Book(tmp_path / "journal.sqlite"))
    review = monitor.desk._mt5_review(NOW)["review"]
    assert review["bot"]["trades"] == 2  # positions 11 and 12; 13 was "tradebot manual"
    assert review["own"]["trades"] == 1
