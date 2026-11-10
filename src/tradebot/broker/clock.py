"""Broker server time <-> UTC.

MT5 stamps bars, ticks and deals in the broker's *server* time. Most FX brokers, MetaQuotes'
own servers included, keep that clock at New York time + 7 hours, so the trading day ends at
00:00 server time: UTC+2 in the northern winter, UTC+3 while New York observes daylight saving.
Converting a year of history with today's single offset labels every summer bar one hour late.

``ServerClock`` is either a fixed offset from UTC or a fixed offset from New York local time.
``detect_convention`` decides which from the broker's own history: the FX week opens at 17:00
New York, so on a New York-following server every week opens at the same server hour, while on
a fixed-offset server the opening hour moves twice a year.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

NEW_YORK = "America/New_York"


def new_york_offset(utc: datetime) -> timedelta:
    """New York's UTC offset at ``utc`` (naive UTC): -5h in winter, -4h under daylight saving."""
    return pd.Timestamp(utc).tz_localize("UTC").tz_convert(NEW_YORK).utcoffset()


@dataclass(frozen=True)
class ServerClock:
    hours: float
    """Server time minus UTC (fixed), or server time minus New York local time (follows_new_york)."""
    follows_new_york: bool = False

    def offset_at(self, utc: datetime) -> timedelta:
        """Server time minus UTC at a given UTC instant."""
        base = timedelta(hours=self.hours)
        return base + new_york_offset(utc) if self.follows_new_york else base

    def describe(self, utc: datetime) -> str:
        now = self.offset_at(utc).total_seconds() / 3600
        if not self.follows_new_york:
            return f"UTC{now:+.2f}h (fixed)"
        return f"UTC{now:+.2f}h now: New York time {self.hours:+g}h, follows US daylight saving"

    def to_utc(self, server: pd.Series) -> pd.Series:
        """Vectorised: naive server timestamps -> naive UTC."""
        if not self.follows_new_york:
            return server - pd.Timedelta(hours=self.hours)
        local = server - pd.Timedelta(hours=self.hours)
        # Ambiguous/nonexistent New York hours fall on Sunday mornings, when FX is closed.
        utc = local.dt.tz_localize(
            NEW_YORK, ambiguous=np.ones(len(local), dtype=bool), nonexistent="shift_forward"
        )
        return utc.dt.tz_convert("UTC").dt.tz_localize(None)

    def epoch_to_utc(self, server_epoch: float) -> datetime:
        server = datetime.fromtimestamp(server_epoch, timezone.utc).replace(tzinfo=None)
        return self.to_utc(pd.Series([pd.Timestamp(server)])).iloc[0].to_pydatetime()

    def utc_to_server_epoch(self, utc: datetime) -> int:
        return int((utc + self.offset_at(utc)).replace(tzinfo=timezone.utc).timestamp())


def week_opens(server_times: Iterable[float]) -> list[pd.Timestamp]:
    """Server timestamp of the first bar after each weekend gap (input: epoch seconds, server time)."""
    times = np.sort(np.asarray(list(server_times), dtype=float))
    if len(times) < 2:
        return []
    gaps = np.flatnonzero(np.diff(times) > 30 * 3600) + 1
    return list(pd.to_datetime(times[gaps], unit="s"))


def detect_convention(server_times: Iterable[float], offset_now: timedelta, utc_now: datetime) -> ServerClock:
    """Pick the clock that explains the broker's week opens; fall back to the measured offset.

    Needs history on both sides of a US daylight-saving change, otherwise the two conventions
    look identical and the measured (fixed) offset is kept. ``server_utc_offset_hours`` in the
    config always forces a fixed offset.
    """
    fixed = ServerClock(offset_now.total_seconds() / 3600)
    opens = week_opens(server_times)
    if len(opens) < 20:
        return fixed
    # Classify each week by New York daylight saving (the server/UTC gap is at most a few hours).
    dst = [new_york_offset((t - offset_now).to_pydatetime()) == timedelta(hours=-4) for t in opens]
    if all(dst) or not any(dst):
        return fixed
    hours = [t.day_of_week * 24 + t.hour + t.minute / 60 for t in opens]
    _, count = Counter(hours).most_common(1)[0]
    if count / len(hours) < 0.9:  # the opening hour moves with the seasons: a fixed-offset server
        return fixed
    relative = (offset_now - new_york_offset(utc_now)).total_seconds() / 3600
    return ServerClock(relative, follows_new_york=True)
