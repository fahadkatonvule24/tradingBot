"""Alerts through the ntfy app, and the heartbeat that says whether alerts are running at all."""

from __future__ import annotations

import io
import json
import re
import urllib.error
from datetime import datetime

import pytest
from test_advisor_ui import SYMBOL, features, primed
from test_alerts import bare_config, broker, cfg, fake  # noqa: F401  (fixtures)

from tradebot.cli import main
from tradebot.config import ConfigError, config_from_dict
from tradebot.notify import Alerts, Notifier, Ntfy, NtfyError, WhatsApp, _clip, ntfy_call, valid_topic
from tradebot.ui.monitor import Monitor

TOPIC = "tradebot-3f9a1c7b2e4d"
TOKEN = "tk_SECRETTOKEN"


def recording(calls: list):
    def opener(request, timeout):
        payload = json.loads(request.data.decode())
        calls.append({"url": request.full_url, "headers": dict(request.header_items()), **payload})
        return io.BytesIO(b'{"id":"x"}')

    return opener


@pytest.mark.parametrize(
    ("topic", "ok"),
    [(TOPIC, True), ("my_alerts-2", True), ("", False), (None, False), ("has space", False),
     ("x" * 65, False), ("ü-topic", False)],
)  # fmt: skip
def test_topic_names(topic, ok):
    assert valid_topic(topic) is ok


def test_published_as_json_with_a_utf8_title_and_the_token():
    calls: list = []
    ntfy_call("https://ntfy.sh/", TOPIC, "Sell GBPUSD now.", "SELL · GBPUSD", 4, TOKEN, recording(calls))
    (call,) = calls
    assert call["url"] == "https://ntfy.sh/" and call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert (call["topic"], call["title"], call["message"], call["priority"]) == (
        TOPIC,
        "SELL · GBPUSD",
        "Sell GBPUSD now.",
        4,
    )


def test_errors_say_why_and_never_carry_the_token(caplog):
    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "no", {}, None)

    with pytest.raises(NtfyError, match="250 a day") as error:
        ntfy_call("https://ntfy.sh", TOPIC, "x", token=TOKEN, opener=refused)
    assert TOKEN not in str(error.value)

    def offline(request, timeout):
        raise urllib.error.URLError(f"no route, Authorization: Bearer {TOKEN}")

    with caplog.at_level("WARNING"):
        Ntfy(TOPIC, token=TOKEN, opener=offline, background=False).send("BUY · X\nnow", "signal")
    assert "could not reach ntfy.sh (URLError)" in caplog.text and TOKEN not in caplog.text


def test_clipped_to_the_byte_limit_at_a_character_boundary():
    clipped = _clip("é" * 3000, 3800)
    assert len(clipped.encode("utf-8")) <= 3800 and clipped.endswith("...")


def test_first_line_is_the_title_and_the_kind_sets_the_priority():
    calls: list = []
    channel = Ntfy(TOPIC, opener=recording(calls), background=False)
    channel.send("SELL · GBPUSD\nSell GBPUSD now.", "signal")
    channel.send("NEWS · USD\nPayrolls in 30 min", "news")
    channel.send("FORMING · GBPUSD\nnot at the important level", "forming")
    channel.send("RUNNING · tradebot\nWatching", "status")
    assert [(c.get("title"), c["priority"]) for c in calls] == [
        ("SELL · GBPUSD", 4), ("NEWS · USD", 3), ("RUNNING · tradebot", 3),
    ]  # fmt: skip


def test_all_channels_from_the_environment():
    env = {"NTFY_TOPIC": TOPIC, "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
           "WHATSAPP_PHONE": "+256700123456", "WHATSAPP_APIKEY": "k"}  # fmt: skip
    alerts = Alerts.from_env(env, background=False, opener=lambda *a, **k: io.BytesIO(b"{}"))
    assert alerts.names == ["ntfy", "Telegram", "WhatsApp +256…456"]
    assert [type(c) for c in alerts.channels] == [Ntfy, Notifier, WhatsApp]
    assert not Alerts.from_env({"NTFY_TOPIC": "not a topic!"}, background=False).enabled


def test_the_dashboard_sends_a_signal_to_ntfy(cfg, broker):  # noqa: F811
    calls: list = []
    alerts = Alerts([Ntfy(TOPIC, opener=recording(calls), background=False)])
    primed(
        Monitor(cfg, broker, [SYMBOL], notifier=alerts), features({"signal": 1, "raw_signal": 1})
    ).refresh()
    (call,) = calls
    assert call["title"] == f"BUY · {SYMBOL}" and call["priority"] == 4


def test_heartbeat_started_then_running_once_each_trading_day(cfg, broker, fake):  # noqa: F811
    calls: list = []
    alerts = Alerts([Ntfy(TOPIC, opener=recording(calls), background=False)])
    monitor = primed(Monitor(cfg, broker, [SYMBOL], notifier=alerts), features())
    monitor.heartbeat = True
    monitor.refresh()
    monitor.refresh()
    assert [c["title"] for c in calls] == ["STARTED · tradebot"]
    assert "if one is missing, alerts are not running" in calls[0]["message"]
    monitor._heartbeat(datetime(2026, 3, 5, 9, 0))  # the next trading day, in session
    monitor._heartbeat(datetime(2026, 3, 5, 15, 0))
    monitor._heartbeat(datetime(2026, 3, 7, 9, 0))  # Saturday: nothing
    assert [c["title"] for c in calls] == ["STARTED · tradebot", "RUNNING · tradebot"]
    quiet = primed(Monitor(cfg, broker, [SYMBOL], notifier=alerts), features())
    quiet.refresh()  # a Monitor sends no heartbeat unless `tradebot ui` turns it on
    assert len(calls) == 2


def test_trading_all_day_sends_the_running_message_at_the_london_open_not_midnight(cfg, broker, fake):  # noqa: F811
    """Regression: with session "all" the day opened at 00:00 UTC, so the phone buzzed at 3 a.m."""
    from dataclasses import replace

    calls: list = []
    alerts = Alerts([Ntfy(TOPIC, opener=recording(calls), background=False)])
    all_day = replace(cfg, schedule=replace(cfg.schedule, session="all"))
    monitor = primed(Monitor(all_day, broker, [SYMBOL], notifier=alerts), features())
    monitor.heartbeat = True
    monitor.refresh()
    monitor._heartbeat(datetime(2026, 3, 5, 0, 30))
    monitor._heartbeat(datetime(2026, 3, 5, 2, 0))
    assert [c["title"] for c in calls] == ["STARTED · tradebot"]
    monitor._heartbeat(datetime(2026, 3, 5, 8, 5))  # London opens 08:00 UTC in March (GMT)
    assert [c["title"] for c in calls] == ["STARTED · tradebot", "RUNNING · tradebot"]


def test_the_ntfy_level_is_checked_in_the_config():
    assert config_from_dict({"alerts": {"ntfy": "all"}}, env={}).alerts.ntfy == "all"
    with pytest.raises(ConfigError, match=r"alerts\.ntfy"):
        config_from_dict({"alerts": {"ntfy": "loud"}}, env={})


def test_setup_saves_a_topic_then_tests_it(bare_config, monkeypatch, capsys, tmp_path):  # noqa: F811
    for name in ("NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN"):  # restored afterwards even if .env sets them
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    (tmp_path / ".env").write_text("MT5_LOGIN=1", encoding="utf-8")
    assert main(["-c", bare_config, "ntfy"]) == 1
    topic = re.search(r"tradebot-[0-9a-f]{12}", capsys.readouterr().out).group(0)
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert saved.startswith("MT5_LOGIN=1\n") and f"NTFY_TOPIC={topic}" in saved
    sent: list = []
    monkeypatch.setattr(
        "tradebot.notify.ntfy_call", lambda server, t, message, **kw: sent.append((server, t))
    )
    monkeypatch.setenv("NTFY_TOPIC", topic)  # the next run reads it from .env (the test guard blocks that)
    assert main(["-c", bare_config, "ntfy"]) == 0
    assert sent == [("https://ntfy.sh", topic)]


def test_an_unexpected_delivery_error_does_not_end_alerts():
    """Regression: an error outside the expected ones (http.client.IncompleteRead) killed the
    sending thread, and every later alert waited in the queue for ever."""
    import http.client
    import threading

    from tradebot.notify import Ntfy

    sent, calls = [], []
    done = threading.Event()

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            if not calls:
                calls.append(1)
                raise http.client.IncompleteRead(b"")
            return b""

    def opener(request, timeout):
        sent.append(request)
        if len(sent) == 2:
            done.set()
        return Reply()

    channel = Ntfy("tradebot-test", opener=opener, gap_seconds=0)
    channel.send("first\nbody", "signal")
    channel.send("second\nbody", "signal")
    assert done.wait(5), "the second alert was never sent"
    channel.close()
