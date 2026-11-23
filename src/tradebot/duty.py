"""Which process manages trades placed by hand and sends the reversal warnings.

The dashboard (``tradebot ui``) and the trading loop (``tradebot run``) can both do it, and both
running at once used to mean every reversal warning reached the phone twice; with the dashboard
alone, restarting it left the open trades unmanaged until it was back. Now exactly one process
does it at a time, decided by operating-system file locks in the working folder:

- the dashboard holds ``tradebot.dashboard.lock`` while it runs (a second dashboard cannot, and
  stays quiet), and takes ``tradebot.manager.lock`` as soon as it is free;
- the trading loop takes ``tradebot.manager.lock`` only while no dashboard runs, and gives it up
  on its next cycle once one does.

The operating system releases a lock when its process ends, however it ends (closed, crashed,
killed), so a restart never leaves a stale lock behind and no clock or heartbeat is involved.
While the dashboard restarts, the trading loop carries on within one of its cycles.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

DASHBOARD_LOCK = "tradebot.dashboard.lock"
MANAGER_LOCK = "tradebot.manager.lock"
LOOP_LOCK = "tradebot.loop.lock"
"""Held by ``tradebot run --live`` while it runs: its engine manages the bot's own trades (and the
dashboard's Get in and Duplicate orders, which carry the bot's number). While nobody holds it, the
dashboard manages those too, so they are never left without breakeven and the trail."""


class FileLock:
    """An exclusive, non-blocking lock on a file, held until ``release`` or the process ends."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> bool:
        """True if this object holds the lock now (taking it if it is free)."""
        if self._file is not None:
            return True
        try:
            handle = open(self.path, "a+b")  # noqa: SIM115 - kept open while the lock is held
        except OSError:
            log.debug("cannot open %s", self.path, exc_info=True)
            return False
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:
            log.debug("unlock of %s failed", self.path, exc_info=True)
        handle.close()

    def free(self) -> bool:
        """True if nobody holds the lock (it is taken and released at once to find out)."""
        if self._file is not None:
            return False
        if not self.acquire():
            return False
        self.release()
        return True


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # a byte past the end may be locked

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Duty:
    """``held()`` once per cycle says whether this process should manage trades placed by hand
    and send reversal warnings now. ``dashboard=True`` for ``tradebot ui``, False for the loop."""

    def __init__(self, folder: str | Path, dashboard: bool, trading: bool = False) -> None:
        """``trading``: this is the trading loop sending orders (``--live``), which manages the
        bot's own trades for as long as it runs."""
        folder = Path(folder)
        self.dashboard = dashboard
        self._dashboard = FileLock(folder / DASHBOARD_LOCK)
        self._manager = FileLock(folder / MANAGER_LOCK)
        self._loop = FileLock(folder / LOOP_LOCK)
        if trading and not dashboard and not self._loop.acquire():
            raise ValueError(
                "another live trading loop is running in this folder; stop it before starting another"
            )
        self._was: bool | None = None

    def set_trading(self, on: bool) -> bool:
        """The trading loop's orders were stopped (or allowed again) after it started: while they
        are stopped it does not manage the bot's trades, so the dashboard does. Return whether
        this loop owns the lock: resuming orders also requires exclusive ownership."""
        if self.dashboard:
            return False
        if on:
            return self._loop.acquire()
        self._loop.release()
        return False

    def trading_loop_running(self) -> bool:
        """A live trading loop runs, and manages the bot's own trades."""
        return self._loop.held or not self._loop.free()

    @property
    def primary(self) -> bool:
        """For a dashboard: it is the only one running (a second one sends nothing to the phone)."""
        return not self.dashboard or self._dashboard.held

    def held(self) -> bool:
        if self.dashboard:
            on = self._dashboard.acquire() and self._manager.acquire()
        elif self._dashboard.free():
            on = self._manager.acquire()
        else:  # a dashboard is running: it takes over on its next refresh
            self._manager.release()
            on = False
        if on != self._was:
            self._was = on
            who = "this dashboard" if self.dashboard else "the trading loop"
            log.info(
                "%s %s the trades placed by hand and the reversal warnings",
                who,
                "now manages" if on else "leaves to the other tradebot process",
            )
        return on

    def holder_text(self) -> str:
        """Who manages, as the dashboard sees it, for its status line."""
        if self._manager.held:
            return "this dashboard"
        if self.dashboard and not self._dashboard.held:
            return "the other dashboard that is already running"
        return "the trading loop (tradebot run)"

    def close(self) -> None:
        self._manager.release()
        self._dashboard.release()
        self._loop.release()
