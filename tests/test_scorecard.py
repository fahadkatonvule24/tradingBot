"""The bot's scorecard, the weekly report, and the heads-up when a trade is placed by hand."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from conftest import FakeMT5
from test_alerts import Inbox

from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams
from tradebot.review import ReviewedTrade, scorecard
from tradebot.ui.monitor import Monitor

NOW = datetime(2026, 3, 4, 12, 7)


def trades(n: int, r: float) -> list[ReviewedTrade]:
    return [ReviewedTrade(ticket=i, symbol="EURUSD", side=1, opened=NOW, closed=NOW, pnl=15 * r,
                          risk_amount=15.0, manual=False, exit="other") for i in range(n)]  # fmt: skip


def test_the_scorecard_reads_at_fifty_and_judges_at_a_hundred():
    assert scorecard([])["text"].startswith("0 of 50 bot trades so far (no R yet): too few to judge")
    early = scorecard(trades(12, 0.4))
    assert early["measured"] == 12 and "12 of 50 bot trades so far (+0.40R a trade): too few" in early["text"]
    assert "a first read, losing so far" in scorecard(trades(60, -0.1))["text"]
    assert "positive over 100+ trades" in scorecard(trades(120, 0.05))["text"]
    assert "Do not take these rules to real money" in scorecard(trades(120, -0.02))["text"]


def monitor() -> tuple[Monitor, Inbox]:
    fake = FakeMT5(now=NOW)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    config = BotConfig(symbols=("EURUSDm",), timeframe="M15",
                       schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""))  # fmt: skip
    inbox = Inbox()
    return Monitor(config, broker, ["EURUSDm"], notifier=inbox), inbox


def held(ticket: int, side: int = 1, magic: int = 0):
    return SimpleNamespace(ticket=ticket, symbol="EURUSDm", side=side, volume=0.02, magic=magic)


def row(action: str, side: int):
    return {
        "symbol": "EURUSDm",
        "advice": SimpleNamespace(action=action, side=side, headline=f"{action} headline"),
    }


def test_a_trade_placed_by_hand_without_a_bot_entry_gets_one_heads_up():
    m, inbox = monitor()
    m._heads_up([held(1)], [row("TREND", 1)], NOW)  # open when the dashboard started: not news
    assert inbox.sent == []
    m._heads_up([held(1), held(2)], [row("TREND", 1)], NOW)
    (message,) = inbox.sent
    assert message.startswith("NO SIGNAL · EURUSDm") and "it showed: TREND headline" in message
    assert "the bot takes no trade on EURUSDm" in message
    m._heads_up([held(1), held(2)], [row("TREND", 1)], NOW)  # said once
    assert len(inbox.sent) == 1


def test_no_heads_up_for_a_trade_the_dashboard_backed_or_for_the_bots_own():
    m, inbox = monitor()
    m._heads_up([], [], NOW)
    m._heads_up([held(3, side=-1)], [row("JOIN", -1)], NOW)  # joined the trend the dashboard offered
    m._heads_up([held(3, side=-1), held(4)], [row("BUY", 1)], NOW)
    m._heads_up([held(3, side=-1), held(4), held(5, magic=m.config.magic)], [row("TREND", 1)], NOW)
    m._heads_up([held(6)], [{"symbol": "EURUSDm"}], NOW)  # no advice to compare with
    assert inbox.sent == []


def test_the_weekly_report_goes_out_once_on_saturday():
    m, inbox = monitor()
    review = {"days": 90, "bot": scorecard(trades(12, 0.4)), "own": {"trades": 34, "avg_r": 0.02}}
    m.desk = SimpleNamespace(state=lambda now: {"review": review})
    m._weekly(datetime(2026, 3, 6, 9, 0))  # Friday
    m._weekly(datetime(2026, 3, 7, 7, 0))  # Saturday, before 08:00 UTC
    assert inbox.sent == []
    m._weekly(datetime(2026, 3, 7, 8, 30))
    m._weekly(datetime(2026, 3, 7, 18, 0))
    (message,) = inbox.sent
    assert message.startswith("WEEKLY · tradebot") and "12 of 50 bot trades so far" in message
    assert "Your own trades, last 90 days: 34, +0.02R each." in message
    m._weekly(datetime(2026, 3, 14, 8, 30))  # the next Saturday
    assert len(inbox.sent) == 2
