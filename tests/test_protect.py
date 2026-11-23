"""Automatic breakeven on every open trade, the ones placed by hand included (tradebot.protect)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import FakeMT5
from test_alerts import Inbox

from tradebot.broker.base import Account, Position
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, ManagementParams, MT5Login, ScheduleParams
from tradebot.data import preset_spec
from tradebot.protect import (
    RETRY_SECONDS,
    STOP_GRACE_SECONDS,
    Bar,
    BreakevenKeeper,
    breakeven_gate,
    breakeven_move,
    move_text,
    partial_close,
    reversal_close,
    stop_for,
    trailing_move,
)
from tradebot.ui.monitor import Monitor

NOW = datetime(2026, 3, 4, 12, 7)
CLOSED = NOW + timedelta(minutes=15)
"""A bar that closed after the trades in these tests opened (an earlier one says nothing about them)."""
SYMBOL = "EURUSDm"
M = ManagementParams()
SPEC = preset_spec("EURUSD")


def position(side=1, entry=1.0950, first=1.0900, sl=None, ticket=7) -> Position:
    return Position(ticket=ticket, symbol="EURUSD", side=side, volume=0.1, price_open=entry,
                    sl=first if sl is None else sl, tp=0.0, time_open=NOW, initial_volume=0.1,
                    initial_sl=first, magic=0)  # fmt: skip


# --- the decision ---------------------------------------------------------------------------------


def test_the_stop_moves_to_entry_plus_a_tenth_of_r_at_one_r():
    assert breakeven_move(position(), SPEC, 1.0999, 1.1000, M) is None  # 0.98R: not yet
    move = breakeven_move(position(), SPEC, 1.1000, 1.1001, M)
    assert move.stop == pytest.approx(1.0955) and move.profit_r == pytest.approx(1.0)
    sell = breakeven_move(position(-1, 1.1000, 1.1050), SPEC, 1.0949, 1.0950, M)  # judged at the ask
    assert sell.stop == pytest.approx(1.0995)


def test_a_stop_is_never_loosened_or_moved_twice():
    assert breakeven_move(position(sl=1.0955), SPEC, 1.1010, 1.1011, M) is None  # already there
    assert breakeven_move(position(sl=1.0980), SPEC, 1.1010, 1.1011, M) is None  # trailed further: kept


def test_no_first_stop_means_no_1r_and_nothing_is_moved():
    assert breakeven_move(position(first=0.0, sl=0.0), SPEC, 1.1100, 1.1101, M) is None
    assert breakeven_move(position(first=1.0940), SPEC, 1.1100, 1.1101, M) is not None  # a tight stop
    assert breakeven_move(position(first=1.1000), SPEC, 1.1100, 1.1101, M) is None  # "stop" above a buy


def test_the_broker_minimum_distance_is_respected():
    close = replace(SPEC, stops_level=500)  # 50 pips: the new stop would be inside it
    assert breakeven_move(position(), close, 1.1000, 1.1001, M) is None


def test_the_gate_needs_the_switch_algo_trading_and_permission_for_real_money():
    on = BotConfig(management=ManagementParams(auto_breakeven=True))
    demo = Account(100, 100, 0, "USD", False, 100)
    real = replace(demo, is_real=True)
    assert not breakeven_gate(BotConfig(), demo, True).on  # off unless switched on
    assert breakeven_gate(on, demo, True).on
    assert "algo trading" in breakeven_gate(on, demo, False).reason
    assert "REAL" in breakeven_gate(on, real, True).reason
    assert breakeven_gate(replace(on, allow_real_account=True), real, True).on


def test_a_refused_change_is_retried_later_not_every_second():
    clock = [0.0]
    keeper = BreakevenKeeper(clock=lambda: clock[0])
    sent = []
    broker = SimpleNamespace(
        symbol=lambda name: SPEC,
        tick=lambda name: SimpleNamespace(bid=1.1000, ask=1.1001),
        modify=lambda p, sl, tp, magic: (
            sent.append(sl) or SimpleNamespace(ok=False, message="trade disabled")
        ),
    )
    config = BotConfig(management=ManagementParams(auto_breakeven=True))
    keeper.run(broker, [position()], config)
    keeper.run(broker, [position()], config)
    assert len(sent) == 1
    clock[0] = RETRY_SECONDS + 1
    keeper.run(broker, [position()], config)
    assert len(sent) == 2


# --- on the dashboard, with a phone trade on the terminal ----------------------------------------------


@pytest.fixture
def fake() -> FakeMT5:
    fake = FakeMT5(now=NOW)  # quotes 1.10000 / 1.10012
    fake.positions = [SimpleNamespace(ticket=41, magic=0, symbol=SYMBOL, type=0, volume=0.02, price_open=1.0950,
                                      sl=1.0900, tp=0.0, time=fake.server_epoch(NOW), profit=10.0, comment="")]  # fmt: skip
    fake.orders = [SimpleNamespace(position_id=41, time_setup=0, volume_initial=0.02, sl=1.0900)]
    return fake


def monitor_for(fake: FakeMT5, auto: bool, inbox: Inbox) -> Monitor:
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    config = BotConfig(symbols=(SYMBOL,), timeframe="M15", management=ManagementParams(auto_breakeven=auto),
                       schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""))  # fmt: skip
    return Monitor(config, broker, [SYMBOL], notifier=inbox)


def test_the_dashboard_moves_a_phone_trades_stop_and_says_so(fake):
    inbox = Inbox()
    monitor = monitor_for(fake, True, inbox)
    monitor.cycle()
    (request,) = [r for r in fake.sent if r["action"] == fake.TRADE_ACTION_SLTP]
    assert (request["position"], request["sl"]) == (41, pytest.approx(1.0955))
    state = monitor.snapshot()["breakeven"]
    assert (state["on"], state["reason"], state["moved"], state["at_r"], state["buffer_r"]) == (
        True,
        "on",
        1,
        1.0,
        0.1,
    )
    assert (state["breakeven"], state["trailing"]) == (True, False)
    (message,) = [m for m in inbox.sent if m.startswith("BREAKEVEN")]
    assert "1.09000 -> 1.09550" in message and "+1.0R in profit" in message


def test_nothing_is_sent_while_the_switch_is_off(fake):
    monitor = monitor_for(fake, False, Inbox())
    monitor.cycle()
    assert not [r for r in fake.sent if r["action"] == fake.TRADE_ACTION_SLTP]
    assert monitor.snapshot()["breakeven"]["on"] is False


def test_a_trade_on_a_symbol_outside_the_watchlist_is_protected_too(fake):
    """Every position on the account is covered, not only the dashboard's symbols."""
    fake.symbols["GBPUSDm"] = SimpleNamespace(
        **(vars(fake.symbols[SYMBOL]) | {"name": "GBPUSDm", "currency_base": "GBP"})
    )
    fake.positions.append(SimpleNamespace(**(vars(fake.positions[0]) | {"ticket": 42, "symbol": "GBPUSDm"})))
    fake.orders.append(SimpleNamespace(**(vars(fake.orders[0]) | {"position_id": 42})))
    monitor = monitor_for(fake, True, Inbox())
    monitor.cycle()
    moved = {r["position"]: r["symbol"] for r in fake.sent if r["action"] == fake.TRADE_ACTION_SLTP}
    assert moved == {41: SYMBOL, 42: "GBPUSDm"}


def test_stops_are_still_protected_when_the_analysis_fails(fake, monkeypatch):
    """Regression: a failed refresh (one bad symbol) skipped the breakeven for every trade."""
    monitor = monitor_for(fake, True, Inbox())
    monkeypatch.setattr(monitor, "refresh", lambda: (_ for _ in ()).throw(ValueError("bad bar")))
    monitor.cycle()
    assert [r for r in fake.sent if r["action"] == fake.TRADE_ACTION_SLTP]


def test_the_message_states_the_configured_buffer_not_a_fixed_tenth():
    from tradebot.protect import move_text

    move = breakeven_move(position(), SPEC, 1.1000, 1.1001, replace(M, breakeven_buffer_r=0.0))
    text = move_text(move, SPEC, True)
    assert "about +0.0R" in text and "+0.1R" not in text


# --- trailing ---------------------------------------------------------------------------------------

TRAIL = ManagementParams(auto_trailing=True)  # from 1.5R, 2 ATR behind the last close


def test_the_trail_starts_at_one_and_a_half_r_two_atr_behind_the_close():
    p = position()  # buy 1.0950, first stop 1.0900: 1R = 50 pips
    assert trailing_move(p, SPEC, 1.1020, 0.0010, 1.1020, 1.1021, TRAIL) is None  # 1.4R: not yet
    move = trailing_move(p, SPEC, 1.1050, 0.0010, 1.1050, 1.1051, TRAIL)  # 2R
    assert move.kind == "trail" and move.stop == pytest.approx(1.1030) and move.profit_r == pytest.approx(2.0)
    sell = trailing_move(position(-1, 1.1000, 1.1050), SPEC, 1.0900, 0.0010, 1.0899, 1.0900, TRAIL)
    assert sell.stop == pytest.approx(1.0920)


def test_the_trail_only_tightens_and_leaves_breakeven_to_breakeven():
    assert (
        trailing_move(position(sl=1.1040), SPEC, 1.1050, 0.0010, 1.1050, 1.1051, TRAIL) is None
    )  # stop is better
    wide = trailing_move(
        position(), SPEC, 1.1030, 0.0050, 1.1030, 1.1031, TRAIL
    )  # 1.6R; 2 ATR back is 1.0930
    assert wide is None  # 1.0930 is behind entry + 0.1R (1.0955): not the trail's job
    assert (
        trailing_move(position(first=0.0, sl=0.0), SPEC, 1.1100, 0.0010, 1.11, 1.1101, TRAIL) is None
    )  # no stop


def test_the_keeper_sends_the_tighter_of_breakeven_and_trail_and_says_which():
    sent = []
    broker = SimpleNamespace(
        symbol=lambda name: SPEC,
        tick=lambda name: SimpleNamespace(bid=1.1050, ask=1.1051),
        modify=lambda p, sl, tp, magic: sent.append(sl) or SimpleNamespace(ok=True, message="done"),
    )
    both = BotConfig(management=ManagementParams(auto_breakeven=True, auto_trailing=True))
    ((move, _),) = BreakevenKeeper().run(broker, [position()], both, {"EURUSD": Bar(1.1050, 0.0010, CLOSED)})
    assert move.kind == "trail" and sent == [pytest.approx(1.1030)]
    text = move_text(move, SPEC, True)
    assert "Stop trailed" in text and "2.0 ATR behind the last close" in text and "about +1.6R" in text
    sent.clear()
    ((move, _),) = BreakevenKeeper().run(broker, [position()], both)  # no bar for it: breakeven only
    assert move.kind == "breakeven" and sent == [pytest.approx(1.0955)]
    sent.clear()
    only_trail = BotConfig(management=ManagementParams(auto_trailing=True))
    assert BreakevenKeeper().run(broker, [position()], only_trail) == [] and sent == []
    assert breakeven_gate(only_trail, Account(100, 100, 0, "USD", False, 100), True).on


def test_the_dashboard_gives_the_keeper_each_instruments_last_closed_bar(fake, monkeypatch):
    monitor = monitor_for(fake, True, Inbox())
    monitor.cycle()  # loads the bars
    seen = {}
    monkeypatch.setattr(
        monitor._breakeven,
        "run",
        lambda broker, held, config, bars=None, *rest: seen.update(bars or {}) or [],
    )
    monitor._keep_breakeven()
    frame = monitor._features[SYMBOL]
    assert seen[SYMBOL] == (
        pytest.approx(float(frame["close"].iat[-1])),
        pytest.approx(float(frame["atr"].iat[-1])),
        (frame["time"].iat[-1] + pd.Timedelta(minutes=15)).to_pydatetime(),  # when it closed
    )


# --- a stop for a trade without one, half off, and closing in profit on a reversal ------------------

from tradebot.config import RiskParams  # noqa: E402

LATER = NOW + timedelta(seconds=STOP_GRACE_SECONDS + 1)


def bare(side=1, entry=1.0950, volume=0.02, initial_volume=0.02):
    return Position(ticket=9, symbol="EURUSD", side=side, volume=volume, price_open=entry, sl=0.0, tp=0.0,
                    time_open=NOW, initial_volume=initial_volume, initial_sl=None, magic=0)  # fmt: skip


def test_a_trade_without_a_stop_gets_one_after_the_grace_period():
    risk = RiskParams()  # sl_atr 1.5
    assert (
        stop_for(bare(), SPEC, 0.0020, 1.0960, 1.0961, risk, NOW + timedelta(seconds=30)) is None
    )  # too new
    move = stop_for(bare(), SPEC, 0.0020, 1.0960, 1.0961, risk, LATER)
    assert (move.kind, move.stop) == ("stop", pytest.approx(1.0920))  # 1.5 ATR under the entry
    gone = stop_for(
        bare(), SPEC, 0.0020, 1.0910, 1.0911, risk, LATER
    )  # already 40 pips down: 1.0920 is above
    assert (gone.kind, gone.stop) == ("stop_from_price", pytest.approx(1.0880))
    assert stop_for(position(), SPEC, 0.0020, 1.0960, 1.0961, risk, LATER) is None  # it has a stop
    assert "had no stop. Stop added at 1.09200" in move_text(move, SPEC, True)


def test_a_reversal_closes_the_trade_only_while_it_is_in_profit():
    assert reversal_close(position(), 1.1000, 1.1000, 1.1001, "warn", "A sign").kind == "reversal"
    assert reversal_close(position(), 1.1000, 1.1000, 1.1001, "", "") is None  # nothing against it
    assert reversal_close(position(), 1.0940, 1.0940, 1.0941, "exit", "x") is None  # losing: left to its stop
    assert reversal_close(position(), 1.0960, 1.0945, 1.0946, "warn", "x") is None  # the live price is a loss
    sell = reversal_close(position(-1, 1.1000, 1.1050), 1.0950, 1.0949, 1.0950, "warn", "x")
    assert sell.volume == 0.1 and sell.profit_r == pytest.approx(1.0)


def test_half_off_at_one_and_a_half_r_once_and_never_on_a_single_lot():
    m = ManagementParams()  # 1.5R, half
    two = replace(position(), volume=0.02, initial_volume=0.02)
    assert partial_close(two, SPEC, 1.1020, m) is None  # 1.4R
    half = partial_close(two, SPEC, 1.1030, m)
    assert (half.kind, half.volume) == ("partial", 0.01)
    assert partial_close(replace(two, volume=0.01), SPEC, 1.1030, m) is None  # already halved
    assert partial_close(replace(position(), volume=0.01, initial_volume=0.01), SPEC, 1.1030, m) is None
    assert "Half off: closed 0.01 of 0.02 lots" in move_text(half, SPEC, True)


def test_the_keeper_adds_a_stop_first_closes_on_a_reversal_then_halves():
    calls = []
    broker = SimpleNamespace(
        symbol=lambda name: SPEC,
        tick=lambda name: SimpleNamespace(bid=1.1030, ask=1.1031),
        modify=lambda p, sl, tp, magic: (
            calls.append(("modify", p.ticket, sl)) or SimpleNamespace(ok=True, message="")
        ),
        close=lambda p, volume, magic, comment: (
            calls.append(("close", p.ticket, volume)) or SimpleNamespace(ok=True, message="")
        ),
    )
    everything = BotConfig(management=ManagementParams(auto_breakeven=True, auto_trailing=True, auto_partial=True,
                                                       auto_reverse_exit=True, auto_stop=True))  # fmt: skip
    two = replace(position(ticket=7), volume=0.02, initial_volume=0.02)  # 1.6R up
    bars = {"EURUSD": Bar(1.1030, 0.0010, CLOSED)}
    keeper = BreakevenKeeper()
    keeper.run(broker, [bare(), two], everything, bars, {7: ("warn", "Averages crossed")}, LATER)
    assert calls == [("modify", 9, pytest.approx(1.0935)), ("close", 7, None)]  # stop added; closed in profit
    calls.clear()
    keeper.run(broker, [two], everything, bars, {7: ("", "")}, LATER)
    assert calls == [("close", 7, 0.01)]  # no reversal: half off at 1.6R
    calls.clear()
    keeper.run(broker, [replace(two, volume=0.01)], everything, bars, {}, LATER)
    assert calls[0][0] == "modify"  # halved already: breakeven / trail from here
    off = BotConfig(management=ManagementParams(auto_breakeven=True))
    calls.clear()
    keeper.run(broker, [bare(), two], off, bars, {7: ("warn", "x")}, LATER)
    assert all(c[0] == "modify" for c in calls) and ("close", 7, None) not in calls


def test_the_bots_own_trades_are_left_to_the_trading_loop():
    """Regression: with `tradebot run --live` managing its trades too, both would close half."""
    calls = []
    broker = SimpleNamespace(
        symbol=lambda name: SPEC,
        tick=lambda name: SimpleNamespace(bid=1.1030, ask=1.1031),
        modify=lambda p, sl, tp, magic: calls.append("modify") or SimpleNamespace(ok=True, message=""),
        close=lambda p, volume, magic, comment: calls.append("close") or SimpleNamespace(ok=True, message=""),
    )
    config = BotConfig(management=ManagementParams(auto_breakeven=True, auto_partial=True))
    bots = replace(position(), magic=config.magic, volume=0.02, initial_volume=0.02)
    bar = {"EURUSD": Bar(1.1030, 0.0010, CLOSED)}
    assert BreakevenKeeper().run(broker, [bots], config, bar) == [] and calls == []
    BreakevenKeeper().run(broker, [replace(bots, magic=0)], config, bar)
    assert calls == ["close"]  # the same trade placed by hand is managed
