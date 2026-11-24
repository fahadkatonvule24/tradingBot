"""The trading loop's heartbeat, which scripts/start_bot.ps1 reads to restart a frozen loop.

On 23 Nov 2026 a MetaTrader 5 call hung for 2 h 18 min after the terminal reconnected; the
process stayed alive, so the autostart task took it for a running loop."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

from test_alerts import Inbox

from tradebot.config import BotConfig
from tradebot.live import FROZEN_NOTE, HEARTBEAT_FILE, LiveRunner


class QuietBroker:
    """Just enough broker for a cycle over no symbols."""

    def positions(self, scope=None):
        return []


def runner_for(tmp_path, heartbeat=True) -> tuple[LiveRunner, Inbox]:
    inbox = Inbox()
    engine = SimpleNamespace(execute=False, events=[])
    path = tmp_path / HEARTBEAT_FILE if heartbeat else None
    return LiveRunner(BotConfig(), QuietBroker(), engine, [], notifier=inbox, heartbeat=path), inbox


def run_once(runner: LiveRunner) -> None:
    # off the main thread: run() would otherwise take over pytest's Ctrl+C handler
    worker = threading.Thread(target=runner.run, kwargs={"once": True})
    worker.start()
    worker.join(10)
    assert not worker.is_alive()


def test_the_loop_writes_its_heartbeat_when_it_starts_and_after_each_cycle(tmp_path):
    runner, inbox = runner_for(tmp_path)
    beat = tmp_path / HEARTBEAT_FILE
    beat.write_text("old\n")
    os.utime(beat, (0, 0))  # a beat left by an earlier loop
    run_once(runner)
    text = beat.read_text()
    assert text.endswith(f" pid {os.getpid()}\n") and text[:4].isdigit()
    assert beat.stat().st_mtime > 0
    assert inbox.sent == []  # no freeze, nothing to say


def test_a_restart_after_a_freeze_is_sent_to_the_phone_once(tmp_path):
    note = tmp_path / FROZEN_NOTE
    note.write_text("﻿The trading loop froze: no cycle for 12 min (last at 17:20).\n", encoding="utf-8")
    runner, inbox = runner_for(tmp_path)
    run_once(runner)
    assert inbox.sent == ["TRADING · The trading loop froze: no cycle for 12 min (last at 17:20)."]
    assert inbox.kinds == ["failed"]
    assert not note.exists()
    again, inbox = runner_for(tmp_path)
    run_once(again)
    assert inbox.sent == []


def test_without_a_heartbeat_path_nothing_is_written_or_read(tmp_path):
    (tmp_path / FROZEN_NOTE).write_text("froze")
    runner, inbox = runner_for(tmp_path, heartbeat=False)
    run_once(runner)
    assert not (tmp_path / HEARTBEAT_FILE).exists()
    assert (tmp_path / FROZEN_NOTE).exists() and inbox.sent == []


def test_a_heartbeat_that_cannot_be_written_never_stops_the_loop(tmp_path):
    runner, _ = runner_for(tmp_path)
    runner.heartbeat = tmp_path / "missing folder" / HEARTBEAT_FILE
    run_once(runner)  # no exception
