"""Phone alerts are tried again while the internet is down (24 Nov 2026 audit).

They were tried once: on a line that dropped 33 times in one night, an "order opened" or
"get out" alert could vanish while nobody was at the PC."""

from __future__ import annotations

import io
import threading
import urllib.error

import pytest

from tradebot import notify
from tradebot.notify import Ntfy, WhatsApp

KEY = "123456"


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(notify, "RETRY_SECONDS", (0.01, 0.01, 0.01))


def flaky(failures: int, calls: list, done: threading.Event, error=None):
    """An opener that fails ``failures`` times, then delivers."""

    def opener(request, timeout):
        calls.append(request)
        if len(calls) <= failures:
            raise error or urllib.error.URLError("network is unreachable")
        done.set()
        return io.BytesIO(b"Message queued")

    return opener


def test_an_ntfy_alert_goes_out_once_the_connection_is_back(caplog):
    calls, done = [], threading.Event()
    channel = Ntfy("tradebot-test", opener=flaky(2, calls, done), gap_seconds=0)
    with caplog.at_level("INFO", logger="tradebot.notify"):
        channel.send("ORDER · EURUSDm\nsell 0.06", "order")
        assert done.wait(5)
        channel.close()
    assert len(calls) == 3
    assert caplog.text.count("not sent yet") == 1  # the outage is reported once, not per try
    assert "sent on try 3" in caplog.text and "dropped" not in caplog.text


def test_a_whatsapp_alert_is_retried_too():
    calls, done = [], threading.Event()
    channel = WhatsApp("+256700123456", KEY, opener=flaky(1, calls, done), gather_seconds=0, gap_seconds=0)
    channel.send("GET OUT · USDJPYm", "exit")
    assert done.wait(5)
    channel.close()
    assert len(calls) == 2


def test_an_alert_is_dropped_with_a_warning_when_the_line_stays_down(caplog):
    calls, done = [], threading.Event()
    channel = Ntfy("tradebot-test", opener=flaky(99, calls, done), gap_seconds=0)
    with caplog.at_level("INFO", logger="tradebot.notify"):
        channel.send("ORDER · EURUSDm\nsell 0.06", "order")
        for _ in range(500):
            if "dropped" in caplog.text:
                break
            threading.Event().wait(0.01)
        channel.close()
    assert len(calls) == 1 + len(notify.RETRY_SECONDS)
    assert "ntfy alert dropped" in caplog.text and "ORDER · EURUSDm" in caplog.text


def test_a_refusal_that_waiting_cannot_fix_is_not_retried(caplog):
    calls, done = [], threading.Event()
    refused = urllib.error.HTTPError("https://ntfy.sh/", 400, "Bad Request", {}, None)
    channel = Ntfy("tradebot-test", opener=flaky(99, calls, done, error=refused), gap_seconds=0)
    with caplog.at_level("INFO", logger="tradebot.notify"):
        channel.send("ORDER · EURUSDm\nsell 0.06", "order")
        for _ in range(300):
            if "alert not sent" in caplog.text:
                break
            threading.Event().wait(0.01)
        channel.close()
    assert len(calls) == 1
    assert "ntfy answered 400" in caplog.text and "dropped" not in caplog.text


def test_a_later_alert_waits_its_turn_behind_a_retried_one():
    calls, done = [], threading.Event()
    channel = Ntfy("tradebot-test", opener=flaky(2, calls, threading.Event()), gap_seconds=0)
    original = channel._deliver
    order = []

    def recording(text, kind="info", retrying=False):
        result = original(text, kind, retrying)
        if result:
            order.append(text)
            if len(order) == 2:
                done.set()
        return result

    channel._deliver = recording
    channel.send("first", "order")
    channel.send("second", "order")
    assert done.wait(5)
    channel.close()
    assert order == ["first", "second"]


def test_a_direct_send_reports_a_failure_at_once_without_retrying(caplog):
    calls = []
    Ntfy("tradebot-test", opener=flaky(99, calls, threading.Event()), background=False).send("x", "order")
    assert len(calls) == 1 and "ntfy alert not sent: could not reach ntfy.sh" in caplog.text
