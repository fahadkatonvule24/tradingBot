"""High-impact news blackouts from a small JSON calendar (times in UTC)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsEvent:
    time: datetime  # naive UTC
    currency: str
    impact: str
    title: str


class NewsCalendar:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._mtime: float | None = None
        self._events: list[NewsEvent] = []

    def events(self) -> list[NewsEvent]:
        if self.path is None or not self.path.is_file():
            return []
        mtime = self.path.stat().st_mtime
        if mtime != self._mtime:  # reload when the file is edited while the bot runs
            self._events = self._load()
            self._mtime = mtime
        return self._events

    def _load(self) -> list[NewsEvent]:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read news calendar %s: %s", self.path, exc)
            return []
        events = []
        for item in raw:
            try:
                when = datetime.fromisoformat(str(item["time_utc"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                log.warning("skipping malformed news event: %s", item)
                continue
            if when.tzinfo is not None:
                when = when.astimezone(timezone.utc).replace(tzinfo=None)
            events.append(
                NewsEvent(
                    when,
                    str(item.get("currency", "")).upper(),
                    str(item.get("impact", "")).lower(),
                    str(item.get("event", "")),
                )
            )
        return events

    def blackout(
        self, now: datetime, currencies: set[str], before_min: int, after_min: int
    ) -> NewsEvent | None:
        for event in self.events():
            if event.impact not in {"high", "red"} or event.currency not in currencies:
                continue
            if event.time - timedelta(minutes=before_min) <= now <= event.time + timedelta(minutes=after_min):
                return event
        return None
