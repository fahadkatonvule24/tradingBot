"""Every BUY, SELL, SKIP and JOIN on the dashboard says what the trading bot does about it.

25 Nov 2026: the dashboard said "Sell XAUUSDm now" and "you can still join the sell" on GBPUSD.
The bot took neither: gold is signals only, joins are off, and the bot's own GBPUSD sell had been
held back by the open-risk limit (a hand trade used it up) with no word on screen or phone."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

from test_advisor_ui import NOW, SYMBOL, advice_for, features, primed
from test_advisor_ui import broker as broker
from test_advisor_ui import cfg as cfg
from test_advisor_ui import fake as fake
from test_alerts import Inbox

from tradebot.advisor import Advice
from tradebot.duty import Duty
from tradebot.ui.monitor import Monitor

BUY = {"signal": 1, "raw_signal": 1}


def card(monitor: Monitor) -> dict:
    monitor.refresh()
    state = monitor.snapshot()
    json.dumps(state, allow_nan=False)
    return state["symbols"][0]["advice"]


def signals_only(cfg):
    return replace(cfg, overrides={SYMBOL: {"risk": {"auto_trade": False}}})


def test_an_auto_traded_signal_says_the_bot_places_it(cfg, broker):
    advice = card(primed(Monitor(cfg, broker, [SYMBOL]), features(BUY)))
    assert advice["action"] == "BUY"
    assert advice["bot"] == "Bot: places this trade itself."
    assert advice["detail"].endswith("Bot: places this trade itself.")  # the card and the alert show it


def test_a_signals_only_symbol_says_the_bot_does_not_trade_it(cfg, broker):
    advice = card(primed(Monitor(signals_only(cfg), broker, [SYMBOL]), features(BUY)))
    assert advice["action"] == "BUY"
    assert advice["bot"] == f"Bot: does not trade {SYMBOL} (signals only); place it yourself if you want it."


def test_without_a_live_trading_loop_nothing_places_it(cfg, broker, tmp_path):
    dashboard = Duty(tmp_path, dashboard=True)
    try:
        advice = card(primed(Monitor(cfg, broker, [SYMBOL], duty=dashboard), features(BUY)))
        assert advice["bot"].startswith("Bot: no live trading loop is running")
        loop = Duty(tmp_path, dashboard=False, trading=True)  # `tradebot run --live` starts
        try:
            advice = card(primed(Monitor(cfg, broker, [SYMBOL], duty=dashboard), features(BUY)))
            assert advice["bot"] == "Bot: places this trade itself."
        finally:
            loop.close()
    finally:
        dashboard.close()


def test_a_held_back_signal_says_so_and_reaches_the_phone(cfg, broker):
    asia = replace(cfg, schedule=replace(cfg.schedule, session="asia"))  # 12:00 UTC: outside its hours
    inbox = Inbox()
    advice = card(primed(Monitor(asia, broker, [SYMBOL], notifier=inbox), features(BUY)))
    assert advice["action"] == "SKIP"
    assert advice["bot"].startswith("Bot: does not place it while that limit holds.")
    assert inbox.kinds == ["skipped"]  # not "blocked", which a phone set to important never hears
    assert "Bot: does not place it" in inbox.sent[0]


def test_a_held_back_signal_of_a_signals_only_symbol_stays_quiet(cfg, broker):
    asia = replace(signals_only(cfg), schedule=replace(cfg.schedule, session="asia"))
    inbox = Inbox()
    card(primed(Monitor(asia, broker, [SYMBOL], notifier=inbox), features(BUY)))
    assert inbox.kinds == ["blocked"]


def test_a_join_says_whether_the_bot_joins(cfg, broker):
    join = Advice(SYMBOL, "JOIN", "Uptrend: you can still join the buy", "The bot signalled BUY 2 bars ago.",
                  1, True, NOW, NOW + timedelta(minutes=15))  # fmt: skip
    off = Monitor(cfg, broker, [SYMBOL])
    assert not cfg.strategy.auto_join
    assert off.bot_line(SYMBOL, join).startswith("Bot: does not join missed signals (auto-join is off)")
    assert off.bot_line(SYMBOL, join, offer_get_in=True).endswith('"Get in" places it as your own trade.')
    on = Monitor(replace(cfg, strategy=replace(cfg.strategy, auto_join=True)), broker, [SYMBOL])
    assert on.bot_line(SYMBOL, join) == "Bot: joins it itself, once, unless a limit holds it back."
    held = replace(join, blocker="open risk would exceed 3.0%")
    assert (
        on.bot_line(SYMBOL, held) == "Bot: does not join while a limit holds (open risk would exceed 3.0%)."
    )


def test_a_signal_the_bot_already_traded_is_not_called_held_back(cfg, broker):
    again = Advice(SYMBOL, "SKIP", "Buy signal, but skip it", "duplicate: already entered on this bar",
                   1, False, NOW, None, blocker="duplicate: EURUSDm was already entered on this bar")  # fmt: skip
    assert (
        Monitor(cfg, broker, [SYMBOL]).bot_line(SYMBOL, again) == "Bot: already took a trade on this signal."
    )


def test_cards_without_a_trade_say_nothing_about_the_bot(cfg, broker):
    monitor = Monitor(cfg, broker, [SYMBOL])
    for action in ("WAIT", "TREND", "CLOSED", "BLOCKED"):
        assert monitor.bot_line(SYMBOL, Advice(SYMBOL, action, "h", "d", 0, False, NOW, None)) == ""


def test_the_loop_tells_the_phone_when_it_holds_back_its_own_signal(cfg, tmp_path):
    from types import SimpleNamespace

    from tradebot.engine import SIGNALS_ONLY, Event
    from tradebot.live import LiveRunner, held_back_signal

    held = "sell signal skipped: open risk would exceed 3.0%"
    assert held_back_signal(held)
    assert held_back_signal("join of the sell signal skipped: daily loss limit: -38.68 (-3.6%)")
    assert not held_back_signal("data: quote is stale (more than 120 seconds old)")
    assert not held_back_signal(f"sell signal skipped: positions: 1 open on XAUUSDm ({SIGNALS_ONLY})")
    assert not held_back_signal(
        "buy signal skipped: duplicate: EURUSDm was already entered on this bar; one entry per bar"
    )

    def cycle(duty_held: bool) -> Inbox:
        inbox = Inbox()
        events = [Event(NOW, "skip", "GBPUSDm", held), Event(NOW, "skip", "EURUSDm", "data: quote is stale")]
        engine = SimpleNamespace(execute=True, events=events)  # a loop that trades: a dry run says nothing
        broker = SimpleNamespace(positions=lambda scope=None: [])
        duty = SimpleNamespace(held=lambda: duty_held, set_trading=lambda on: True)  # this loop owns the lock
        LiveRunner(cfg, broker, engine, [], notifier=inbox, duty=duty).cycle()
        return inbox

    alone = cycle(duty_held=True)  # no dashboard: the loop speaks
    assert alone.sent == [
        "GBPUSDm: sell signal skipped: open risk would exceed 3.0%. The bot did not place it."
    ]
    assert alone.kinds == ["skipped"]
    assert cycle(duty_held=False).sent == []  # a dashboard runs and sends its own


def test_a_closed_market_says_market_closed_even_when_its_quote_is_stale(cfg, broker, fake):
    fake.utc_now = NOW + timedelta(hours=9)  # the bars stop at 12:00; the quote is old as well
    fake.symbol_info_tick = lambda name: type(
        "T", (), {"time": fake.server_epoch(NOW), "bid": 1.1, "ask": 1.10012}
    )()
    advice = advice_for(cfg, broker, features(BUY), now=NOW + timedelta(hours=9))
    assert (advice.action, advice.headline) == ("CLOSED", "Market closed")
