"""Economic news: a calendar you maintain, and the week's calendar pulled from a public feed.

Times are UTC. The engine pauses new entries around high-impact events for an instrument's
currencies; the dashboard lists what is coming and warns before it.

The feed (``schedule.news_feed``, the ForexFactory weekly calendar by default in config.json)
is downloaded by ``NewsFeed`` on a background thread into a JSON file (``news_feed_file``),
and ``NewsCalendar`` reads that file next to your own. Only the dashboard and the trading loop
start the download; a backtest never touches the network. The request carries no account data.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FEED_REFRESH = timedelta(hours=4)
FEED_RETRY = timedelta(minutes=30)
HIGH = {"high", "red"}


@dataclass(frozen=True)
class NewsEvent:
    time: datetime  # naive UTC
    currency: str
    impact: str
    title: str
    forecast: str = ""
    previous: str = ""


class NewsError(RuntimeError):
    pass


def _utc(text: Any) -> datetime:
    when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def parse_events(raw: Any) -> list[NewsEvent]:
    """Events from our own file format (``time_utc``, ``currency``, ``event``) or the feed's
    (``date`` with its offset, ``country``, ``title``, ``forecast``, ``previous``)."""
    if not isinstance(raw, list):
        raise NewsError("expected a list of events")
    events = []
    for item in raw:
        try:
            when = _utc(item.get("time_utc") or item["date"])
            title = str(item.get("event") or item.get("title") or "")
        except (AttributeError, KeyError, TypeError, ValueError):
            log.warning("skipping malformed news event: %s", item)
            continue
        events.append(
            NewsEvent(
                when,
                str(item.get("currency") or item.get("country") or "").upper(),
                str(item.get("impact", "")).lower(),
                title,
                str(item.get("forecast") or ""),
                str(item.get("previous") or ""),
            )
        )
    return sorted(events, key=lambda e: e.time)


class NewsCalendar:
    """Your own file plus the downloaded one; each is re-read when it changes on disk."""

    def __init__(self, path: str | Path | None, feed_path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.feed_path = Path(feed_path) if feed_path else None
        self._files: dict[Path, tuple[int, list[NewsEvent]]] = {}

    def _read(self, path: Path | None) -> list[NewsEvent]:
        if path is None:
            return []
        cached = self._files.get(path)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return cached[1] if cached is not None else []
        if cached is None or cached[0] != mtime:  # reload when the file is edited while the bot runs
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                events = parse_events(raw)
                if raw and not events:
                    raise NewsError("no valid events could be read")
            except (OSError, ValueError, NewsError) as exc:
                log.warning("could not read news calendar %s: %s", path, exc)
                events = cached[1] if cached is not None else []
            cached = (mtime, events)
            self._files[path] = cached
        return cached[1]

    def events(self) -> list[NewsEvent]:
        seen, out = set(), []
        for event in [*self._read(self.path), *self._read(self.feed_path)]:
            key = (event.time, event.currency, event.title.lower())
            if key not in seen:
                seen.add(key)
                out.append(event)
        return sorted(out, key=lambda e: e.time)

    def updated(self) -> datetime | None:
        """When the downloaded calendar was last written (UTC), or None without one."""
        if self.feed_path is None or not self.feed_path.is_file():
            return None
        return datetime.fromtimestamp(self.feed_path.stat().st_mtime, timezone.utc).replace(tzinfo=None)

    def blackout(
        self, now: datetime, currencies: set[str], before_min: int, after_min: int
    ) -> NewsEvent | None:
        for event in self.events():
            if event.impact not in HIGH or event.currency not in currencies:
                continue
            if event.time - timedelta(minutes=before_min) <= now <= event.time + timedelta(minutes=after_min):
                return event
        return None

    def upcoming(
        self,
        now: datetime,
        currencies: Iterable[str],
        hours: float = 36,
        impacts: Iterable[str] = ("high", "medium"),
        since_minutes: int = 30,
    ) -> list[NewsEvent]:
        """Events for these currencies from ``since_minutes`` ago to ``hours`` ahead."""
        wanted, levels = set(currencies), {i.lower() for i in impacts}
        start, end = now - timedelta(minutes=since_minutes), now + timedelta(hours=hours)
        return [
            e for e in self.events() if e.currency in wanted and e.impact in levels and start <= e.time <= end
        ]


class NewsFeed:
    """Keeps ``path`` filled with the feed's calendar: on start if it is old, then every 4 hours."""

    def __init__(
        self,
        url: str,
        path: str | Path,
        opener: Callable = urllib.request.urlopen,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    ) -> None:
        self.url, self.path, self._opener, self._clock = url, Path(path), opener, clock
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh(self) -> int:
        """Download now and write the file. Returns the number of events; raises NewsError."""
        request = urllib.request.Request(self.url, headers={"User-Agent": "tradebot news calendar"})
        try:
            with self._opener(request, timeout=20) as response:
                raw = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            hint = " (asked too often: it is retried later)" if exc.code == 429 else ""
            raise NewsError(f"the news feed answered {exc.code}{hint}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NewsError(f"could not reach the news feed ({type(exc).__name__})") from None
        except ValueError:
            raise NewsError("the news feed sent something that is not JSON") from None
        events = parse_events(raw)
        if raw and not events:
            raise NewsError("the news feed's format has changed: no event could be read")
        rows = [{**asdict(e), "time_utc": e.time.isoformat() + "Z", "event": e.title} for e in events]
        for row in rows:
            row.pop("time"), row.pop("title")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)  # the calendar never reads a half-written file
        self.error = None
        return len(events)

    def age(self) -> timedelta | None:
        if not self.path.is_file():
            return None
        written = datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc).replace(tzinfo=None)
        return self._clock() - written

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="news-feed", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            age = self.age()
            wait = FEED_REFRESH - age if age is not None and age < FEED_REFRESH else timedelta(0)
            if wait <= timedelta(0):
                try:
                    count = self.refresh()
                    log.info("news calendar updated: %d events this week", count)
                    wait = FEED_REFRESH
                except NewsError as exc:
                    if str(exc) != self.error:  # the same failure every half hour is logged once
                        log.warning("news calendar not updated: %s", exc)
                    self.error = str(exc)
                    wait = FEED_RETRY
            self._stop.wait(wait.total_seconds())
