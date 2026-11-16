"""The setup's record on stored history, measured in a separate process.

A ten-year backtest of six instruments takes about 11 seconds of pure-Python work. Run inside the
dashboard it competed with every click for the interpreter lock: each setup or timeframe switch
started one, so a few clicks in a row kept a core busy and the page answered slowly. A child
process at below-normal priority leaves the dashboard and the MT5 terminal responsive, a result
is kept per setup and timeframe so switching back is instant, and a newer switch stops a run
whose result nobody is waiting for.

    python -m tradebot.ui.track CONFIG.pickle DATA_DIR NAMES_JSON    # prints the record as JSON
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from tradebot.config import BotConfig

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 900


def measure(config: BotConfig, data_dir: str | Path, names: dict[str, str]) -> dict[str, Any]:
    """Backtest ``config`` on the CSVs in ``data_dir``. ``names`` maps the broker's symbol names
    to the configured ones the files are named after. Raises FileNotFoundError or ValueError when
    there is no usable history."""
    from tradebot.backtest import run_backtest
    from tradebot.data import load_dataset, load_trend_bars
    from tradebot.ui.monitor import clean

    data, specs = load_dataset(Path(data_dir), list(config.symbols), config.timeframe)
    htf = load_trend_bars(Path(data_dir), config)
    result = run_backtest(config, data, specs, htf=htf)
    per, m = result.per_symbol, result.metrics
    return clean(
        {
            "start": result.start,
            "end": result.end,
            "trades": m.trades,
            "return_pct": m.return_pct,
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor,
            "avg_r": m.avg_r,
            "symbols": {
                broker_name: {
                    "trades": int(per.loc[configured, "trades"]),
                    "win_rate": float(per.loc[configured, "win_rate"]),
                    "profit_factor": float(per.loc[configured, "profit_factor"]),
                    "avg_r": float(per.loc[configured, "avg_r"]),
                }
                for broker_name, configured in names.items()
                if configured in per.index
            },
        }
    )


class NoRecord(RuntimeError):
    """No usable history (missing files), or the run was stopped because a newer one replaced it."""


class TrackRecords:
    """One measuring process at a time, results kept per (setup, timeframe)."""

    def __init__(
        self, command: Callable[[Path, str | Path, dict[str, str]], Sequence[str]] | None = None
    ) -> None:
        self._command = command or _child_command
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    def cached(self, config: BotConfig) -> dict[str, Any] | None:
        return self._cache.get(_key(config))

    def get(self, config: BotConfig, data_dir: str | Path, names: dict[str, str]) -> dict[str, Any]:
        """The record for ``config``: from the cache, or measured now in a child process (blocks
        the calling thread, not the interpreter). Raises NoRecord."""
        key = _key(config)
        if key in self._cache:
            return self._cache[key]
        with tempfile.TemporaryDirectory(prefix="tradebot-track-") as folder:
            path = Path(folder) / "config.pickle"
            path.write_bytes(pickle.dumps(config))
            command = list(self._command(path, data_dir, names))
            with self._lock:
                self._stop_running()  # its result is for a setup nobody is looking at any more
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    creationflags=_low_priority(),
                )
                self._process = process
            try:
                out, err = process.communicate(timeout=TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _kill_tree(process)
                raise NoRecord(f"the backtest took longer than {TIMEOUT_SECONDS} s") from None
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None
        if process.returncode != 0:
            reason = (err or "").strip().splitlines()[-1:] or [f"stopped (exit code {process.returncode})"]
            raise NoRecord(reason[0])
        record = json.loads(out)
        self._cache[key] = record
        return record

    def close(self) -> None:
        with self._lock:
            self._stop_running()

    def _stop_running(self) -> None:
        if self._process is not None and self._process.poll() is None:
            _kill_tree(self._process)


def _key(config: BotConfig) -> tuple[str, str]:
    return config.strategy.name, config.timeframe


def _child_command(config_path: Path, data_dir: str | Path, names: dict[str, str]) -> list[str]:
    return [sys.executable, "-m", "tradebot.ui.track", str(config_path), str(data_dir), json.dumps(names)]


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop the child and everything it started. On Windows a virtual environment's python.exe is
    a launcher that runs the real interpreter as its own child: killing only the launcher left
    the backtest running, holding the output pipe, so the caller waited for it to finish."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False)
    else:
        process.kill()
    process.wait(timeout=30)


def _low_priority() -> int:
    """Below-normal priority on Windows, so the dashboard and MT5 always come first."""
    return getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if os.name == "nt" else 0


def main(argv: Sequence[str]) -> int:
    config_path, data_dir, names = argv
    config = pickle.loads(Path(config_path).read_bytes())  # written by TrackRecords.get, just now
    try:
        record = measure(config, data_dir, json.loads(names))
    except (FileNotFoundError, ValueError) as exc:
        print(f"no track record: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
