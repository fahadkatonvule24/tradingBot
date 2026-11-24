"""The trading loop's heartbeat, which scripts/start_bot.ps1 reads to restart a frozen loop.

On 23 Nov 2026 a MetaTrader 5 call hung for 2 h 18 min after the terminal reconnected; the
process stayed alive, so the autostart task took it for a running loop."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_alerts import Inbox

from tradebot import cli
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


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:], root.level = handlers, level


@pytest.mark.parametrize(
    ("error", "code", "text"),
    [
        (ValueError("another live trading loop is running in this folder"), 2,
         "tradebot run did not start: another live trading loop is running in this folder"),
        (RuntimeError("MT5 initialize failed"), 1, "tradebot run stopped on an unexpected error"),
    ],
)  # fmt: skip
def test_a_loop_that_cannot_start_says_why_in_the_log(
    tmp_path, monkeypatch, restore_logging, error, code, text
):
    """Started by the autostart task, the loop's window closes when it exits: on 24 Nov two
    restarts ended within a minute and left no trace of why."""
    config = Path(__file__).resolve().parent.parent / "config" / "config.json"
    monkeypatch.chdir(tmp_path)  # tradebot.log is written in the working folder

    def refuse(*_):
        raise error

    monkeypatch.setattr(cli, "cmd_run", refuse)
    assert cli.main(["-c", str(config), "run", "--live"]) == code
    assert text in (tmp_path / "tradebot.log").read_text(encoding="utf-8")


def test_a_heartbeat_that_cannot_be_written_never_stops_the_loop(tmp_path):
    runner, _ = runner_for(tmp_path)
    runner.heartbeat = tmp_path / "missing folder" / HEARTBEAT_FILE
    run_once(runner)  # no exception
