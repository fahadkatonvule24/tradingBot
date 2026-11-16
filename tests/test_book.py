"""Your own accounts: equity typed in by hand, trades logged from the phone, and what follows from them."""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL, features

from tradebot.book import REFUSAL, Book, BookBroker, BookError
from tradebot.broker.mt5 import MT5Broker
from tradebot.cli import main
from tradebot.config import BotConfig, MT5Login, ScheduleParams
from tradebot.engine import Engine
from tradebot.notify import Notifier
from tradebot.review import closed_trades, summarize
from tradebot.ui.monitor import Monitor
from tradebot.ui.server import TOKEN_HEADER, DashboardServer


@pytest.fixture
def fake() -> FakeMT5:
    return FakeMT5(now=NOW)


@pytest.fixture
def broker(fake: FakeMT5) -> MT5Broker:
    return MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)


@pytest.fixture
def cfg() -> BotConfig:
    return BotConfig(
        symbols=(SYMBOL,), timeframe="M15", schedule=ScheduleParams(session="all", trade_weekends=True, news_file="")
    )  # fmt: skip


@pytest.fixture
def book(tmp_path) -> Book:
    return Book(tmp_path / "journal.sqlite")


class Inbox:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.kinds: list[str] = []

    def send(self, text: str, kind: str = "info") -> None:
        self.sent.append(text)
        self.kinds.append(kind)


def desk_monitor(cfg, broker, book, frame=None, **kwargs) -> Monitor:
    monitor = Monitor(cfg, broker, [SYMBOL], book=book, **kwargs)
    monitor._features[SYMBOL] = features() if frame is None else frame
    monitor._fetched[SYMBOL] = time.monotonic()
    monitor._preview_for = lambda symbol, feats: None
    return monitor


def move_price(fake: FakeMT5, bid: float) -> None:
    fake.symbol_info_tick = lambda name: SimpleNamespace(
        time=fake.server_epoch(fake.utc_now), bid=bid, ask=round(bid + 0.00012, 5)
    )


# --- the book -------------------------------------------------------------------------------------


def test_accounts_are_kept_and_one_is_selected(tmp_path):
    path = tmp_path / "journal.sqlite"
    book = Book(path)
    book.save_account("Exness", 500, NOW)
    book.save_account("XM", 1200, NOW, min_lot=0.1, lot_step=0.1)
    assert book.active() is None  # the terminal's account until you choose one
    book.use("exness")  # names are not case-sensitive
    book.close()
    again = Book(path)
    assert again.active() == "Exness"
    assert [(a.name, a.balance, a.min_lot) for a in again.accounts()] == [
        ("Exness", 500, None),
        ("XM", 1200, 0.1),
    ]
    again.save_account("EXNESS", 650, NOW)  # same account, new equity
    assert again.account("Exness").balance == 650 and len(again.accounts()) == 2
    again.remove_account("Exness")
    assert again.active() is None and [a.name for a in again.accounts()] == ["XM"]


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda b: b.save_account("", 500, NOW), "name"),
        (lambda b: b.save_account("A", 0, NOW), "positive"),
        (lambda b: b.use("nobody"), "no account named"),
        (lambda b: b.open_trade("A", "EURUSD", 1, 0.1, 1.10, NOW, sl=1.11), "stop of a buy must be below"),
        (
            lambda b: b.open_trade("A", "EURUSD", -1, 0.1, 1.10, NOW, tp=1.11),
            "target of a sell must be below",
        ),
        (lambda b: b.open_trade("A", "EURUSD", 1, 0, 1.10, NOW), "lot size"),
        (lambda b: b.close_trade(99, 1.1, 0.0, NOW), "no trade #99"),
    ],
)
def test_the_book_refuses_what_makes_no_sense(book, call, message):
    book.save_account("A", 1000, NOW)
    with pytest.raises(BookError, match=message):
        call(book)


def test_closing_a_trade_moves_the_balance_and_can_be_corrected(book):
    book.save_account("A", 1000, NOW)
    trade = book.open_trade("A", "EURUSD", 1, 0.1, 1.1000, NOW, sl=1.0950, tp=1.1100)
    assert trade.is_open and trade.initial_sl == 1.0950
    assert book.amend(trade.id, 1.1005, 1.1100).initial_sl == 1.0950  # a trailed stop keeps the original 1R
    closed = book.close_trade(trade.id, 1.1005, 5.0, NOW + timedelta(hours=1), "trailing stop")
    assert not closed.is_open and book.account("A").balance == 1005
    book.set_result(trade.id, 3.2)  # the broker's figure, commission included
    assert book.account("A").balance == pytest.approx(1003.2)
    assert book.reopen(trade.id).is_open and book.account("A").balance == pytest.approx(1000)
    with pytest.raises(BookError, match="still has open trades"):
        book.remove_account("A")
    book.delete_trade(trade.id)
    assert book.trades("A") == []


# --- the engine on your own account ---------------------------------------------------------------


def test_sizes_come_from_the_equity_you_typed_in(cfg, broker, book):
    own = BookBroker(broker, book, cfg.magic)
    assert own.account().equity == 10_000  # nothing selected: the terminal's account
    book.save_account("Exness", 2000, NOW)
    book.use("Exness")
    account = own.account()
    assert (account.balance, account.equity, account.currency) == (2000, 2000, "USD")
    engine = Engine(cfg, own, execute=False, live=True)
    row = features({"signal": 1, "raw_signal": 1}).iloc[-1]
    plan = engine.plan_entry(SYMBOL, own.symbol(SYMBOL), cfg.for_symbol(SYMBOL), row, 0.00012, 1)
    # 0.25% of 2,000 = 5.00 at risk; a 15-pip stop costs 150 per lot -> 0.03 lots, 4.50 at risk
    assert plan.volume == 0.03 and plan.risk_amount == pytest.approx(4.5)
    assert plan.equity == 2000


def test_an_account_too_small_for_the_minimum_lot_is_told_so(cfg, broker, book):
    book.save_account("Small", 500, NOW)
    book.use("Small")
    own = BookBroker(broker, book, cfg.magic)
    row = features({"signal": 1, "raw_signal": 1}).iloc[-1]
    plan = Engine(cfg, own, execute=False).plan_entry(
        SYMBOL, own.symbol(SYMBOL), cfg.for_symbol(SYMBOL), row, 0.00012, 1
    )
    assert plan.message == "minimum lot would risk 0.30% (> 0.25%)"
    book.save_account("Small", 500, NOW, min_lot=0.001, lot_step=0.001)  # a broker with micro lots
    plan = Engine(cfg, own, execute=False).plan_entry(
        SYMBOL, own.symbol(SYMBOL), cfg.for_symbol(SYMBOL), row, 0.00012, 1
    )
    assert plan.volume == 0.008


def test_logged_trades_are_the_positions_and_the_history(cfg, broker, book):
    book.save_account("A", 2000, NOW)
    book.use("A")
    own = BookBroker(broker, book, cfg.magic)
    trade = book.open_trade("A", SYMBOL, 1, 0.03, 1.10012, NOW, sl=1.09862, tp=1.10312)
    (position,) = own.positions(cfg.magic)
    assert (position.ticket, position.volume, position.sl, position.initial_risk) == (
        trade.id, 0.03, 1.09862, pytest.approx(0.0015))  # fmt: skip
    assert position.profit == pytest.approx(-0.36)  # bought at the ask, valued at the bid 1.10000
    assert own.account().equity == pytest.approx(1999.64)
    status = Engine(cfg, own, execute=False, live=True).risk_status(NOW)
    assert status.positions == 1 and status.open_risk_percent == pytest.approx(4.5 / 1999.64 * 100)
    book.close_trade(trade.id, 1.09862, -4.5, NOW + timedelta(minutes=30), "stop")
    assert [(d.entry, d.profit) for d in own.deals(NOW - timedelta(days=1), cfg.magic)] == [
        ("in", 0.0),
        ("out", -4.5),
    ]
    assert own.positions(cfg.magic) == [] and own.account().balance == 1995.5
    assert own.open(None, cfg.magic).message == REFUSAL  # an order is never sent for your own account
    assert own.margin_required(SYMBOL, 1, 0.03) is None


def test_the_daily_loss_limit_counts_trades_from_the_book(cfg, broker, book, fake):
    book.save_account("A", 2000, NOW)
    book.use("A")
    own = BookBroker(broker, book, cfg.magic)
    for minutes in (10, 20):
        t = book.open_trade(
            "A", SYMBOL, 1, 0.1, 1.1000, NOW - timedelta(hours=2) + timedelta(minutes=minutes), sl=1.0980
        )
        book.close_trade(t.id, 1.0980, -21.0, NOW - timedelta(hours=1) + timedelta(minutes=minutes), "stop")
    status = Engine(cfg, own, execute=False, live=True).risk_status(NOW)
    assert status.day_pnl == -42 and status.day_percent == pytest.approx(-2.1)
    assert status.blocked.startswith("daily loss limit")
    assert status.losing_streak == 2


def test_review_measures_logged_trades_in_r(cfg, broker, book):
    book.save_account("A", 2000, NOW)
    own = BookBroker(broker, book, cfg.magic)
    win = book.open_trade(
        "A", SYMBOL, 1, 0.1, 1.1000, NOW - timedelta(hours=5), sl=1.0980, tp=1.1040, source="signal"
    )
    book.close_trade(win.id, 1.1040, 40.0, NOW - timedelta(hours=4), "target")
    loss = book.open_trade("A", SYMBOL, -1, 0.1, 1.1000, NOW - timedelta(hours=3), sl=1.1020)
    book.close_trade(loss.id, 1.1020, -20.0, NOW - timedelta(hours=2), "stop")
    with own.using("A"):
        trades = closed_trades(own, cfg.magic, NOW - timedelta(days=1))
    assert [(t.r, t.manual, t.exit) for t in trades] == [
        (pytest.approx(2.0), False, "target"),  # logged while the bot was signalling it
        (pytest.approx(-1.0), True, "stop"),  # your own call
    ]
    assert summarize(trades).avg_r == pytest.approx(0.5)


# --- the dashboard --------------------------------------------------------------------------------


def test_saving_an_account_keeps_the_mt5_account_until_you_switch(cfg, broker, book):
    monitor = desk_monitor(cfg, broker, book, features({"signal": 1, "raw_signal": 1}))
    monitor.refresh()
    assert monitor.snapshot()["symbols"][0]["advice"]["plan"]["volume"] == 0.16  # the terminal's 10,000
    out = monitor.desk.op({"op": "account", "name": "Exness", "equity": "2000"})
    assert out == {
        "ok": True,
        "message": "Exness: equity set to 2,000.00 USD. Still using the MetaTrader 5 account; choose Use "
        "(or /use Exness) to size for this one.",
    }
    monitor.refresh()
    state = monitor.snapshot()
    assert state["book"]["active"] is None and state["symbols"][0]["advice"]["plan"]["volume"] == 0.16
    assert state["symbols"][0]["sizes"]["buy"][0] == pytest.approx(
        {"account": "Exness", "equity": 2000, "volume": 0.03, "risk_amount": 4.5, "risk_percent": 0.225,
         "rejected": ""})  # fmt: skip
    assert monitor.desk.op({"op": "use", "name": "Exness"})["ok"]
    monitor.refresh()
    state = monitor.snapshot()
    json.dumps(state, allow_nan=False)
    assert state["account"]["equity"] == 2000 and state["book"]["active"] == "Exness"
    assert state["symbols"][0]["advice"]["plan"]["volume"] == 0.03
    assert state["gate"] == {"execute": False, "reason": REFUSAL, "live_flag": False}
    assert monitor.execute(SYMBOL, 1) == {"ok": False, "message": f"orders are disabled: {REFUSAL}"}


def test_logging_a_trade_copies_the_plan_and_shows_as_a_position(cfg, broker, book):
    monitor = desk_monitor(cfg, broker, book, features({"signal": 1, "raw_signal": 1}))
    monitor.desk.op({"op": "account", "name": "Exness", "equity": 2000})
    monitor.desk.op({"op": "use", "name": "Exness"})
    out = monitor.desk.op({"op": "log", "symbol": "eurusd", "side": "buy"})
    assert out["ok"], out
    assert out["message"] == (
        f"Logged #1: BUY 0.03 {SYMBOL} at 1.10012 on Exness, stop 1.09862, target 1.10312. "
        "At the stop it loses 4.50 USD."
    )
    assert book.trade(1).source == "signal"  # the bot was signalling a buy
    monitor.refresh()
    state = monitor.snapshot()
    (position,) = state["positions"]
    assert (position["ticket"], position["book"], position["sl"]) == (1, True, 1.09862)
    assert position["r"] == pytest.approx(-0.08)  # one spread, in R
    assert state["risk"]["positions"] == 1
    # a fill at another price keeps the plan's distances
    out = monitor.desk.op({"op": "log", "symbol": SYMBOL, "side": -1, "entry": 1.1010, "volume": 0.02})
    assert "SELL 0.02" in out["message"] and "stop 1.10250, target 1.09800" in out["message"]
    assert book.trade(2).source == "own call"


def test_bad_requests_are_explained_not_raised(cfg, broker, book):
    monitor = desk_monitor(cfg, broker, book)
    assert "add one first" in monitor.desk.op({"op": "log", "symbol": SYMBOL, "side": 1})["message"]
    assert monitor.desk.op({"op": "account", "name": "A", "equity": "lots"}) == {
        "ok": False, "message": "equity must be a number, got 'lots'"}  # fmt: skip
    assert "not on the watchlist" in monitor.desk.op({"op": "log", "symbol": "XAUUSD", "side": 1})["message"]
    assert monitor.desk.op({"op": "explode"})["ok"] is False


def test_a_crossed_stop_closes_the_logged_trade_and_says_it_is_assumed(cfg, broker, book, fake):
    inbox = Inbox()
    monitor = desk_monitor(cfg, broker, book, notifier=inbox)
    monitor.desk.op({"op": "account", "name": "Exness", "equity": 2000})
    monitor.desk.op({"op": "use", "name": "Exness"})
    monitor.desk.op({"op": "log", "symbol": SYMBOL, "side": 1})
    monitor.refresh()
    assert len(monitor.snapshot()["positions"]) == 1 and inbox.sent == []
    move_price(fake, 1.09850)  # through the stop at 1.09862
    monitor.refresh()
    state = monitor.snapshot()
    assert state["positions"] == []
    closed = book.trade(1)
    assert (closed.exit_price, closed.exit_reason, closed.assumed) == (1.09862, "stop", True)
    assert closed.pnl == pytest.approx(-4.5) and state["account"]["balance"] == pytest.approx(1995.5)
    (message,) = inbox.sent
    assert message.startswith(
        f"CLOSED · {SYMBOL}\n#1 BUY 0.03 {SYMBOL} on Exness closed at 1.09862 (stop): -4.50 USD (-1.00R)."
    )
    assert "Assumed from the terminal's prices" in message
    assert state["book"]["closed"][0]["r"] == pytest.approx(-1.0)
    assert state["book"]["review"]["all"]["trades"] == 1
    # it was not stopped at your broker after all: reopen it, and it is not closed again on the spot
    move_price(fake, 1.09900)
    assert monitor.desk.op({"op": "reopen", "id": 1})["ok"]
    monitor.refresh()
    assert len(monitor.snapshot()["positions"]) == 1 and book.account("Exness").balance == 2000


def test_a_stop_hit_on_a_closed_bar_is_found_after_a_restart(cfg, broker, book):
    frame = features()
    opened = frame["time"].iloc[-6].to_pydatetime() - timedelta(minutes=1)
    book.save_account("A", 2000, NOW)
    book.open_trade("A", SYMBOL, -1, 0.05, 1.0990, opened, sl=1.1002, tp=1.0960)
    frame.loc[frame.index[-3], "high"] = 1.1003
    monitor = desk_monitor(cfg, broker, book, frame)
    monitor.refresh()
    closed = book.trade(1)
    assert closed.exit_reason == "stop" and closed.closed == frame["time"].iloc[
        -3
    ].to_pydatetime() + timedelta(minutes=15)


def test_an_alert_gives_the_size_at_every_account(cfg, broker, book):
    inbox = Inbox()
    monitor = desk_monitor(cfg, broker, book, features({"signal": 1, "raw_signal": 1}), notifier=inbox)
    monitor.desk.op({"op": "account", "name": "Exness", "equity": 2000})
    monitor.desk.op({"op": "use", "name": "Exness"})
    monitor.desk.op({"op": "account", "name": "XM", "equity": 8000})
    monitor.desk.op({"op": "account", "name": "Tiny", "equity": 300})
    monitor.refresh()
    (message,) = inbox.sent
    assert "0.03 lots · risk 4.50 · target 9.00 · sized for Exness, equity 2,000 USD" in message
    assert "XM (8,000.00 USD): 0.13 lots · risk 19.50 USD" in message
    assert "Tiny (300.00 USD): no size (minimum lot would risk 0.50% (> 0.25%))" in message
    sizes = monitor.snapshot()["symbols"][0]["sizes"]["buy"]
    assert [s["account"] for s in sizes] == ["Tiny", "XM"]  # the selected account is the plan itself


# --- commands from the phone ----------------------------------------------------------------------


def test_telegram_commands_log_and_close_trades(cfg, broker, book, fake):
    monitor = desk_monitor(cfg, broker, book)
    say = monitor.desk.command
    assert say("hello") == ""  # not a command
    assert say("/help").startswith("Commands")
    assert say("/equity Exness 2,000").startswith("Exness: equity set to 2,000.00 USD. Still using")
    assert say("/took eurusd buy lots=0.02").startswith(  # the only account: no need to name it
        f"Logged #1: BUY 0.02 {SYMBOL} at 1.10012 on Exness, stop 1.09862"
    )
    assert say("/sl 1 1.0990") == f"#1 BUY {SYMBOL}: stop 1.09900, target 1.10312."
    monitor.refresh()
    assert say("/open").startswith(
        f"#1 BUY 0.02 {SYMBOL} · Exness · from 1.10012 · SL 1.09900 · TP 1.10312 · -0.24 USD (-0.08R)"
    )
    assert say("/use Exness").startswith("Now using Exness")
    assert "Exness: equity 1,999.76 USD" in say("/status")
    assert say("/accounts") == "> Exness: equity 1,999.76 USD, 1 open, floating -0.24 USD"
    move_price(fake, 1.10200)
    assert (
        say("/close 1")
        == f"#1 BUY 0.02 {SYMBOL} on Exness closed at 1.10200 (closed by hand): +3.76 USD (+1.25R)."
    )
    assert say("/close 1 pnl=3.1").startswith("trade #1 is already closed")
    assert say("/equity Exness 2100") == "Exness: equity set to 2,100.00 USD."
    assert say("/use mt5") == "Now using the MetaTrader 5 account in the terminal."
    assert say("/nonsense").startswith("unknown command /nonsense")
    plan = say("/plan EURUSD sell")
    assert (
        plan.startswith(f"SELL · {SYMBOL}\nYour call, not a bot signal.")
        and "Exness (2,100.00 USD): 0.03 lots" in plan
    )


def test_only_your_own_chat_can_send_commands():
    replies: list[str] = []
    notifier = Notifier("token", "42", background=False, opener=lambda *a, **k: None)
    notifier.send = replies.append
    notifier._handler = lambda text: f"got {text}"
    notifier.handle({"update_id": 1, "message": {"chat": {"id": 7}, "text": "/equity A 1"}})  # a stranger
    notifier.handle({"update_id": 2, "message": {"chat": {"id": 42}, "text": "just chatting"}})
    assert replies == []
    notifier.handle({"update_id": 3, "message": {"chat": {"id": 42}, "text": "/status"}})
    assert replies == ["got /status"]


def test_the_listener_skips_commands_sent_before_it_started():
    served: list[dict] = []
    polls = iter([
        [{"update_id": 8, "message": {"chat": {"id": 42}, "text": "/close 1"}}],  # the backlog
        [{"update_id": 9, "message": {"chat": {"id": 42}, "text": "/open"}}],
    ])  # fmt: skip
    notifier = Notifier("token", "42", background=False, opener=lambda *a, **k: None)
    seen = threading.Event()

    def updates(offset: int, wait: int) -> list[dict]:
        served.append({"offset": offset})
        try:
            return next(polls)
        except StopIteration:
            notifier._closing.set()
            return []

    notifier._updates = updates
    notifier.send = lambda text: seen.set()
    notifier.listen(lambda text: f"ran {text}")
    assert seen.wait(5)
    notifier._listener.join(5)
    assert [s["offset"] for s in served][:2] == [-1, 9]  # /close 1 was never run


def test_book_endpoint_needs_the_session_token(cfg, broker, book):
    monitor = desk_monitor(cfg, broker, book)
    worker = threading.Thread(target=monitor.run, daemon=True)
    server = DashboardServer(monitor, port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    worker.start()
    try:

        def post(headers: dict) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
            body = json.dumps({"op": "account", "name": "Exness", "equity": 750})
            conn.request("POST", "/api/book", body, {"Host": f"127.0.0.1:{server.port}", **headers})
            response = conn.getresponse()
            return response.status, json.loads(response.read())

        assert post({})[0] == 403 and book.accounts() == []
        status, out = post({TOKEN_HEADER: server.token})
        assert status == 200 and out["ok"] and book.account("Exness").balance == 750
    finally:
        server.shutdown()
        monitor.stop()
        worker.join(5)


def test_account_command_needs_no_terminal(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"symbols": ["EURUSD"], "journal": str(tmp_path / "j.sqlite")}), encoding="utf-8"
    )
    assert main(["-c", str(config), "account", "Exness", "500"]) == 0
    assert main(["-c", str(config), "account", "XM", "1200", "--min-lot", "0.1", "--leverage", "500"]) == 0
    out = capsys.readouterr().out
    assert "Exness" in out and "1,200.00" in out and "1:500" in out
    assert "The MetaTrader 5 account is in use" in out  # saving never switches
    book = Book(tmp_path / "j.sqlite")
    assert book.active() is None and book.account("XM").min_lot == 0.1 and book.account("XM").leverage == 500
    book.close()
    assert main(["-c", str(config), "account", "Exness", "--use"]) == 0
    assert "The MetaTrader 5 account is in use" not in capsys.readouterr().out
    assert main(["-c", str(config), "account", "Nobody", "--use"]) == 2  # refused, with the reason


def test_times_in_the_book_survive_the_round_trip(book):
    book.save_account("A", 1000, datetime(2026, 3, 4, 12, 7, 31))
    trade = book.open_trade("A", "EURUSD", 1, 0.1, 1.1, datetime(2026, 3, 4, 12, 7, 31))
    assert trade.opened == datetime(2026, 3, 4, 12, 7, 31) == pd.Timestamp(trade.opened).to_pydatetime()
