"""Company earnings dates, for keeping new stock entries away from the gap a report can cause.

A US stock can open 5-15% away from the last close on the day after its earnings report, far past
any stop. Read from a JSON file of New York dates per stock, e.g.

    {"AAPL": ["2027-01-28"], "NVDA": ["2027-02-24"]}

Companies report before the open or after the close, so the gap is on the report day or the next
one: new entries pause from ``days_before`` trading days before the report date to ``days_after``
trading days after it. A date written "~2027-01-28" is an estimate (companies confirm a few weeks
ahead): its pause is one trading day wider on each side, and the dashboard calls it estimated.
Keys starting with "_" are notes. The file is read again when it changes. Broker names match by
prefix, the longest key first: AAPLm is AAPL.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")


def _shift(day: date, trading_days: int) -> date:
    """``day`` moved by whole weekdays (holidays are not counted: a day early at most)."""
    step = 1 if trading_days >= 0 else -1
    left = abs(trading_days)
    while left:
        day += timedelta(days=step)
        if day.weekday() < 5:
            left -= 1
    return day


class EarningsCalendar:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._stamp: tuple[int, int] | None = None
        self._dates: dict[str, list[date]] = {}
        self._estimated: set[tuple[str, date]] = set()
        self.error: str | None = None

    def _load(self) -> dict[str, list[date]]:
        if self.path is None or not self.path.is_file():
            self._stamp, self._dates = None, {}
            return self._dates
        info = self.path.stat()
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp == self._stamp:
            return self._dates
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected an object of symbol: [dates]")
            dates: dict[str, list[date]] = {}
            estimated: set[tuple[str, date]] = set()
            for symbol, values in raw.items():
                if str(symbol).startswith("_"):
                    continue
                key = str(symbol).upper()
                days = []
                for value in values:
                    text = str(value).strip()
                    day = date.fromisoformat(text.lstrip("~"))
                    days.append(day)
                    if text.startswith("~"):
                        estimated.add((key, day))
                dates[key] = sorted(days)
            self.error = None
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning("earnings dates not read from %s: %s", self.path, exc)
            self.error = f"{self.path} could not be read: {exc}"
            dates, estimated = {}, set()
        self._stamp, self._dates, self._estimated = stamp, dates, estimated
        return dates

    def _key(self, symbol: str) -> str | None:
        name = symbol.upper()
        matches = [k for k in self._load() if name.startswith(k)]
        return max(matches, key=len) if matches else None

    def known(self, symbol: str) -> bool:
        """Whether the file lists any date for ``symbol``."""
        return self._key(symbol) is not None

    def next_report(self, symbol: str, now: datetime) -> date | None:
        """The first report date on or after today in New York, or None."""
        key = self._key(symbol)
        if key is None:
            return None
        today = _new_york_day(now)
        return next((d for d in self._dates[key] if d >= today), None)

    def is_estimated(self, symbol: str, day: date) -> bool:
        """Whether ``day`` is listed for ``symbol`` as an estimate ("~2027-01-28")."""
        key = self._key(symbol)
        return key is not None and (key, day) in self._estimated

    def blackout(self, symbol: str, now: datetime, days_before: int, days_after: int) -> date | None:
        """The report date whose quiet period ``now`` falls in, or None."""
        key = self._key(symbol)
        if key is None:
            return None
        today = _new_york_day(now)
        for report in self._dates[key]:
            wider = 1 if (key, report) in self._estimated else 0
            if _shift(report, -days_before - wider) <= today <= _shift(report, days_after + wider):
                return report
        return None


def _new_york_day(now: datetime) -> date:
    aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return aware.astimezone(NEW_YORK).date()
