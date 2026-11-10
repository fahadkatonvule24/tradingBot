"""Early warning on the forming bar, and alerts to the phone."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import SYMBOL, features

from tradebot.advisor import cross_level, early_warning
from tradebot.broker.mt5 import MT5Broker
from tradebot.cli import main
from tradebot.config import BotConfig, MT5Login, ScheduleParams, StrategyParams
from tradebot.data import preset_spec
from tradebot.engine import Engine
from tradebot.indicators import ema
from tradebot.live import LiveRunner, latest_features, preview_features
from tradebot.notify import Notifier, TelegramError, call, discover_chats
from tradebot.ui.monitor import Monitor

NOW = datetime(2026, 3, 4, 12, 7)


class Inbox:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        pass


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
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


def rising(n: int = 120) -> pd.DataFrame:
    close = pd.Series(1.10 + 0.0001 * np.arange(n))
    return pd.DataFrame({"close": close, "ema_fast": ema(close, 10), "ema_slow": ema(close, 50)})


# --- early warning --------------------------------------------------------------------------------


def test_cross_level_is_the_exact_close_that_crosses_the_averages():
    frame = rising()
    side, level = cross_level(frame, StrategyParams())
    assert side == -1 and level < frame["close"].iloc[-1]  # rising market: the next cross is down

    def gap_after(close: float) -> float:
        series = pd.concat([frame["close"], pd.Series([close])], ignore_index=True)
        return ema(series, 10).iloc[-1] - ema(series, 50).iloc[-1]

    assert gap_after(level + 1e-6) > 0 > gap_after(level - 1e-6)
    assert cross_level(frame, StrategyParams(name="breakout")) is None


def test_early_warning_reports_a_forming_trigger_and_its_filter():
    frame = rising()
    params, spec = BotConfig().for_symbol("EURUSD"), preset_spec("EURUSD")
    building = early_warning(frame, None, params, spec, 15)
    assert building["firing"] is False and building["side"] == -1 and building["level"] is not None

    preview = pd.DataFrame(
        [
            {
                "time": pd.Timestamp("2026-03-04 12:00"),
                "close": 1.105,
                "raw_signal": -1,
                "signal": 0,
                "block": "trend",
            }
        ]
    )
    firing = early_warning(frame, preview, params, spec, 15)
    assert (firing["firing"], firing["side"], firing["passes"]) == (True, -1, False)
    assert firing["blocked_by"] == "against higher-timeframe trend"
    assert firing["closes_at"] == datetime(2026, 3, 4, 12, 15) and firing["price"] == 1.105

    preview.loc[0, ["signal", "block"]] = [-1, ""]
    assert early_warning(frame, preview, params, spec, 15)["passes"] is True


def test_preview_appends_the_forming_bar_without_changing_closed_bars(cfg, broker):
    closed = latest_features(broker, cfg, SYMBOL)
    preview = preview_features(broker, cfg, SYMBOL, closed)
    assert len(preview) == len(closed) + 1
    assert preview["time"].iloc[-1] == datetime(2026, 3, 4, 12, 0)  # the bar still forming at 12:07
    pd.testing.assert_series_equal(preview["signal"].iloc[:-1], closed["signal"], check_names=False)
    broker.forming_bar = lambda *args: None  # market closed: nothing is forming
    assert preview_features(broker, cfg, SYMBOL, closed) is None


def forming_sell(monitor: Monitor, broker, cfg) -> None:
    closed = latest_features(broker, cfg, SYMBOL)
    preview = preview_features(broker, cfg, SYMBOL, closed)
    preview.loc[preview.index[-1], ["raw_signal", "signal", "block"]] = [-1, 0, "trend"]
    monitor._preview_for = lambda symbol, feats: preview


def test_a_forming_trigger_is_announced_once_per_bar(cfg, broker):
    inbox = Inbox()
    monitor = Monitor(cfg, broker, [SYMBOL], notifier=inbox)
    forming_sell(monitor, broker, cfg)
    for _ in range(3):
        monitor.refresh()
    (message,) = inbox.sent
    assert message.startswith(f"FORMING (unconfirmed) · {SYMBOL}\nSELL trigger forming")
    assert "Decided at 12:15 UTC" in message
    assert "the filters would block it (against higher-timeframe trend)" in message
    state = monitor.snapshot()
    assert state["symbols"][0]["advice"]["early"]["firing"] is True
    assert [a["action"] for a in state["activity"]] == ["FORMING"]
    json.dumps(state, allow_nan=False)


# --- alerts from the dashboard and the trading loop -----------------------------------------------


def primed(monitor: Monitor, frame: pd.DataFrame) -> Monitor:
    import time

    monitor._features[SYMBOL] = frame
    monitor._fetched[SYMBOL] = time.monotonic()
    monitor._preview_for = lambda symbol, feats: None
    return monitor


def test_signal_alert_carries_the_plan(cfg, broker):
    inbox = Inbox()
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), features({"signal": 1, "raw_signal": 1}))
    monitor.refresh()
    monitor.refresh()  # the same bar is not announced twice
    (message,) = inbox.sent
    assert message.startswith(f"BUY · {SYMBOL}\nBuy {SYMBOL} now.")
    assert (
        "BUY plan: entry ~1.10012 · SL 1.09862 · TP 1.10312 · 0.16 lots · risk 24.00 · target 48.00"
        in message
    )


def test_blocked_trigger_alert_names_the_filter_and_waiting_is_silent(cfg, broker):
    inbox = Inbox()
    blocked = features({"raw_signal": -1, "block": "rsi"})
    primed(Monitor(cfg, broker, [SYMBOL], notifier=inbox), blocked).refresh()
    (message,) = inbox.sent
    assert message.startswith(f"BLOCKED · {SYMBOL}\nSell trigger fired, blocked by a filter.")
    assert "RSI stretched" in message and "SELL plan: entry ~1.10000 · SL 1.10150 · TP 1.09700" in message

    quiet = Inbox()
    primed(Monitor(cfg, broker, [SYMBOL], notifier=quiet), features()).refresh()
    assert quiet.sent == []


def test_trading_loop_sends_trades_not_skips(cfg, broker):
    inbox = Inbox()
    engine = Engine(cfg, broker, execute=False, live=True)
    runner = LiveRunner(cfg, broker, engine, [SYMBOL], notifier=inbox)

    def process(symbol: str) -> None:
        engine._emit(NOW, "dry_open", symbol, "DRY RUN BUY 0.16 · EMA 10/50 cross up")
        engine._emit(NOW, "skip", symbol, "sell signal skipped: session")

    runner._process = process
    runner.cycle()
    assert inbox.sent == [f"{SYMBOL}: DRY RUN BUY 0.16 · EMA 10/50 cross up"]


# --- Telegram -------------------------------------------------------------------------------------


def replying(payload: dict, calls: list):
    def opener(request, timeout):
        calls.append((request.full_url, request.data))
        return io.BytesIO(json.dumps(payload).encode())

    return opener


def test_notifier_posts_to_the_bot_api():
    calls: list = []
    notifier = Notifier(
        "TOKEN123", "42", opener=replying({"ok": True, "result": {}}, calls), background=False
    )
    notifier.send("BUY EURUSD")
    ((url, data),) = calls
    assert url == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert b"chat_id=42" in data and b"text=BUY+EURUSD" in data


def test_notifier_is_off_without_both_credentials():
    calls: list = []
    opener = replying({"ok": True}, calls)
    assert not Notifier.from_env({}, opener=opener).enabled
    half = Notifier.from_env({"TELEGRAM_BOT_TOKEN": "t"}, opener=opener)
    half.send("nothing happens")
    assert not half.enabled and calls == []
    assert Notifier.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, opener=opener, background=False
    ).enabled


def test_background_delivery_and_close():
    calls: list = []
    notifier = Notifier("t", "1", opener=replying({"ok": True, "result": {}}, calls))
    notifier.send("one")
    notifier.send("two")
    notifier.close()
    assert [data for _, data in calls] == [b"chat_id=1&text=one", b"chat_id=1&text=two"]


def test_failures_never_raise_and_never_leak_the_token(caplog):
    def rejected(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    with pytest.raises(TelegramError, match="401: the bot token was rejected") as error:
        call("SECRET-TOKEN", "sendMessage", {}, rejected)
    assert "SECRET-TOKEN" not in str(error.value)

    def offline(request, timeout):
        raise urllib.error.URLError("no route to host via https://api.telegram.org/botSECRET-TOKEN/")

    with caplog.at_level("WARNING"):
        Notifier("SECRET-TOKEN", "1", opener=offline, background=False).send("hello")  # does not raise
    assert "Telegram alert not sent: could not reach Telegram (URLError)" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text

    with pytest.raises(TelegramError, match="chat not found"):
        call("t", "sendMessage", {}, replying({"ok": False, "description": "chat not found"}, []))


def test_discover_chats_lists_each_chat_once():
    updates = [
        {"message": {"chat": {"id": 99, "first_name": "Kato", "last_name": "F"}}},
        {"message": {"chat": {"id": 99, "first_name": "Kato", "last_name": "F"}}},
        {"channel_post": {"chat": {"id": -100, "title": "Alerts"}}},
        {"edited_message": {}},
    ]
    assert discover_chats("t", replying({"ok": True, "result": updates}, [])) == [
        ("99", "Kato F"),
        ("-100", "Alerts"),
    ]


@pytest.fixture
def bare_config(tmp_path, monkeypatch):
    """A config in a folder with no .env, so the user's real credentials are never read."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"symbols": ["EURUSD"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return str(path)


def test_telegram_command_walks_through_the_setup(bare_config, monkeypatch, capsys):
    assert main(["-c", bare_config, "telegram"]) == 1
    assert "@BotFather" in capsys.readouterr().out

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET-TOKEN")
    monkeypatch.setattr("tradebot.notify.discover_chats", lambda token: [("99", "Kato")])
    assert main(["-c", bare_config, "telegram"]) == 1
    out = capsys.readouterr().out
    assert "TELEGRAM_CHAT_ID=99" in out and "SECRET-TOKEN" not in out

    sent = []
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    monkeypatch.setattr(
        "tradebot.notify.call", lambda token, method, params: sent.append((method, params["chat_id"]))
    )
    assert main(["-c", bare_config, "telegram"]) == 0
    assert sent == [("sendMessage", "99")] and "Test message sent" in capsys.readouterr().out
