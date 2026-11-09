"""Append-only trade journal in SQLite: every order, close, stop move and rejection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    time_utc  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    symbol    TEXT,
    side      TEXT,
    volume    REAL,
    price     REAL,
    sl        REAL,
    tp        REAL,
    ticket    INTEGER,
    pnl       REAL,
    reason    TEXT,
    dry_run   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events(time_utc);
"""


@dataclass(frozen=True)
class JournalEvent:
    time: datetime
    kind: str
    symbol: str | None = None
    side: str | None = None
    volume: float | None = None
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    ticket: int | None = None
    pnl: float | None = None
    reason: str = ""
    dry_run: bool = True


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db = sqlite3.connect(self.path)
        self._db.executescript(SCHEMA)

    def record(self, event: JournalEvent) -> None:
        self._db.execute(
            "INSERT INTO events (time_utc, kind, symbol, side, volume, price, sl, tp, ticket, pnl, reason, dry_run)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.time.isoformat(timespec="seconds"), event.kind, event.symbol, event.side,
                event.volume, event.price, event.sl, event.tp, event.ticket, event.pnl,
                event.reason, int(event.dry_run),
            ),
        )  # fmt: skip
        self._db.commit()

    def summary(self, days: int, now: datetime) -> list[tuple[str, str, int, float]]:
        """(symbol, kind, count, pnl) for the last ``days`` days."""
        since = (now - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self._db.execute(
            "SELECT COALESCE(symbol, '-'), kind, COUNT(*), COALESCE(SUM(pnl), 0) FROM events"
            " WHERE time_utc >= ? GROUP BY 1, 2 ORDER BY 1, 2",
            (since,),
        )
        return list(rows)

    def close(self) -> None:
        self._db.close()
