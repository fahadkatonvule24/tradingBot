"""Reversal check for a position that is already open.

The entry rules say when to get in. A person holding a trade asks the other question: has the
market turned against it? This module answers with the same closed-bar facts the strategy uses,
counted only from the bar after the position opened, so nothing that was already true at the
entry is reported as news:

  signal     the watched setup gave an entry signal the other way and every filter passed it
             (the bot closes its own trades on this: ``close_on_reverse``)
  structure  a bar closed beyond the prior ``breakout_period``-bar extreme, against the position
  average    the fast EMA crossed the slow EMA against the position
  trend      the higher-timeframe trend turned against the position
  failed     the break the position was riding closed back through its level (breakout, retest)

A sign stands until the market takes it back: a close back beyond the broken level, the averages
crossing back, the trend no longer pointing against the trade, a fresh trigger in its favour.

One sign is a warning. The opposite signal, or two signs standing together, is the "get out"
level. ``Watch`` takes one closed bar at a time, so the dashboard, the live loop and the backtest
measurement in ``scripts/research_reversal_exit.py`` all run the same code.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from tradebot.broker.base import Position, SymbolSpec
from tradebot.config import TIMEFRAME_MINUTES, StrategyParams
from tradebot.strategy import trend_columns

WARN, EXIT = "warn", "exit"
ORDER = ("signal", "failed", "structure", "average", "trend")
COLUMNS = ("time", "close", "ema_fast", "ema_slow", "trend", "signal", "raw_signal",
           "range_high", "range_low", "failed_side", "failed_level")  # fmt: skip


@dataclass(frozen=True)
class Sign:
    key: str
    time: Any
    """Open time of the bar that produced it."""
    level: float | None = None


def _number(row: Mapping, key: str) -> float:
    value = row.get(key)
    return float(value) if value is not None and pd.notna(value) else float("nan")


def _whole(row: Mapping, key: str) -> int:
    value = row.get(key)
    return int(value) if value is not None and pd.notna(value) else 0


def _average_state(row: Mapping) -> int:
    gap = _number(row, "ema_fast") - _number(row, "ema_slow")
    return int(np.sign(gap)) if np.isfinite(gap) else 0


class Watch:
    """The evidence against one open position, fed one closed bar at a time.

    ``entry`` is the last bar that closed before the position opened. It sets what was already
    true at the entry (the averages, the higher-timeframe trend) so only a change counts.
    """

    def __init__(self, side: int, entry: Mapping | None = None) -> None:
        self.side = side
        self.signs: dict[str, Sign] = {}
        self.bars = 0
        self._average = _average_state(entry) if entry is not None else 0
        self._trend = _whole(entry, "trend") if entry is not None else 0
        self._seeded = entry is not None

    def update(self, row: Mapping) -> None:
        side, signs, time = self.side, self.signs, row["time"]
        close = float(row["close"])
        average, trend = _average_state(row), _whole(row, "trend")
        if not self._seeded:  # no bar from before the entry: this one is the reference
            self._average, self._trend, self._seeded = average, trend, True
        self.bars += 1

        if _whole(row, "signal") == -side:
            signs["signal"] = Sign("signal", time)
        elif _whole(row, "raw_signal") == side:  # a fresh trigger in the position's favour
            signs.pop("signal", None)
            signs.pop("failed", None)

        for key in ("structure", "failed"):  # a close back beyond the level takes the sign back
            held = signs.get(key)
            if held is not None and (close - held.level) * side > 0:
                del signs[key]
        if "structure" not in signs:
            edge = _number(row, "range_low" if side > 0 else "range_high")
            if np.isfinite(edge) and (close - edge) * side < 0:
                signs["structure"] = Sign("structure", time, edge)
        if _whole(row, "failed_side") == side:
            signs["failed"] = Sign("failed", time, _number(row, "failed_level"))

        if average == -side and self._average != -side:
            signs["average"] = Sign("average", time)
        elif average == side:
            signs.pop("average", None)
        if average:
            self._average = average

        if trend == -side and self._trend != -side:
            signs["trend"] = Sign("trend", time)
        elif trend != -side:
            signs.pop("trend", None)
        self._trend = trend

    @property
    def level(self) -> str:
        if "signal" in self.signs or len(self.signs) >= 2:
            return EXIT
        return WARN if self.signs else ""


def watch_position(features: pd.DataFrame, side: int, opened: datetime, bar_minutes: int) -> Watch:
    """A ``Watch`` brought up to date with every bar in ``features`` that closed after ``opened``.

    A position older than the loaded window is judged from the start of the window.
    """
    closes = (features["time"] + pd.Timedelta(minutes=bar_minutes)).to_numpy()
    moment = pd.Timestamp(opened)
    if moment.tzinfo is not None:
        moment = moment.tz_convert("UTC").tz_localize(None)
    first = max(int(np.searchsorted(closes, moment.to_datetime64(), side="right")), 1)
    columns = [c for c in COLUMNS if c in features.columns]
    rows = features[columns].iloc[first - 1 :].to_dict("records")
    watch = Watch(side, rows[0])
    for row in rows[1:]:
        watch.update(row)
    return watch


@dataclass(frozen=True)
class Reversal:
    """What the dashboard and the alerts show for one position."""

    level: str
    """"" (nothing against the position), "warn" (one sign) or "exit" (get out)."""
    headline: str
    signs: list[dict] = field(default_factory=list)
    """Each: ``key``, ``text`` and ``time`` (when the bar that produced it closed)."""
    forming: list[dict] = field(default_factory=list)
    """Signs the bar still forming would add if it closed at the current price. Not confirmed."""
    forming_level: str = ""
    forming_closes_at: datetime | None = None
    bars: int = 0
    """Bars closed since the position opened."""


def _text(
    sign: Sign, side: int, p: StrategyParams, timeframe: str, spec: SymbolSpec, trends: str, forming: bool
) -> str:
    """One sign in words: what happened, or what would happen if the forming bar closed here."""
    against, word = ("below", "sell") if side > 0 else ("above", "buy")
    price = "" if sign.level is None else f"{sign.level:.{spec.digits}f}"
    closed, crossed, turned, failed, passed = (
        ("would close", "would cross", "would turn", "would fail", "would pass")
        if forming
        else ("closed", "crossed", "turned", "failed", "passed")
    )
    if sign.key == "signal":
        return f"A {word} signal {passed} every filter. The bot closes its own trades on this."
    if sign.key == "failed":
        was = "above" if side > 0 else "below"
        return f"The break {was} {price} {failed}: {timeframe} {closed} back {against} it."
    if sign.key == "structure":
        edge = "low" if side > 0 else "high"
        return f"{timeframe} {closed} {against} the prior {p.breakout_period}-bar {edge} at {price}."
    if sign.key == "average":
        return f"EMA {p.fast_ema} {crossed} {against} EMA {p.slow_ema} on {timeframe}."
    return f"The {trends} trend {turned} {'down' if side > 0 else 'up'}."


def assess(
    features: pd.DataFrame,
    side: int,
    opened: datetime,
    p: StrategyParams,
    timeframe: str,
    spec: SymbolSpec,
    preview: pd.DataFrame | None = None,
    watch: Watch | None = None,
) -> Reversal:
    """The reversal evidence against a position of ``side`` opened at ``opened``.

    ``preview`` is the same features with the forming bar appended at the current price; pass a
    ``watch`` already built for these features to skip rebuilding it.
    """
    bar_minutes = TIMEFRAME_MINUTES[timeframe]
    bar = pd.Timedelta(minutes=bar_minutes)
    if watch is None:
        watch = watch_position(features, side, opened, bar_minutes)
    names = [c[6:] for c in trend_columns(features)] or [p.trend_timeframe]
    trends = " and ".join(names)

    def listed(found: dict[str, Sign], unconfirmed: bool = False) -> list[dict]:
        return [
            {"key": key, "text": _text(found[key], side, p, timeframe, spec, trends, unconfirmed),
             "time": (pd.Timestamp(found[key].time) + bar).to_pydatetime()}
            for key in ORDER if key in found
        ]  # fmt: skip

    forming, forming_level, closes_at = [], "", None
    if preview is not None and len(preview) and preview["time"].iloc[-1] > features["time"].iloc[-1]:
        ahead = copy.deepcopy(watch)
        ahead.update({c: preview[c].iloc[-1] for c in COLUMNS if c in preview.columns})
        new = {key: sign for key, sign in ahead.signs.items() if key not in watch.signs}
        if new:
            forming, forming_level = listed(new, unconfirmed=True), ahead.level
            closes_at = (preview["time"].iloc[-1] + bar).to_pydatetime()

    word = "buy" if side > 0 else "sell"
    count = len(watch.signs)
    if watch.level == EXIT:
        headline = f"Get out: reversal against this {word}"
    elif watch.level == WARN:
        headline = f"Reversal warning on this {word}"
    elif forming:
        headline = f"A reversal sign is forming against this {word}"
    else:
        headline = ""
    if count > 1:
        headline += f" ({count} signs)"
    return Reversal(watch.level, headline, listed(watch.signs), forming, forming_level, closes_at, watch.bars)


def holding_text(position: Position, spec: SymbolSpec, price: float | None) -> str:
    """The position, its result so far and its stop, for an alert a person reads cold."""
    word = "BUY" if position.side > 0 else "SELL"
    digits = spec.digits
    parts = [f"{word} {position.volume:g} {position.symbol} from {position.price_open:.{digits}f}"]
    if price is not None:
        now = f"now {price:.{digits}f}, {position.profit:+,.2f}"
        risk = position.initial_risk
        if risk:
            now += f" ({(price - position.price_open) * position.side / risk:+.1f}R)"
        parts.append(now)
    parts.append(f"stop {position.sl:.{digits}f}" if position.sl else "NO STOP on this position")
    return ", ".join(parts) + "."


class Alerts:
    """Says each new sign once per position, and each forming sign once per bar."""

    def __init__(self) -> None:
        self._seen: dict[int, set[str]] = {}
        self._forming: dict[int, tuple] = {}

    def new(self, ticket: int, reversal: Reversal) -> tuple[list[dict], list[dict]]:
        """(confirmed signs not announced yet, forming signs not announced for this bar)."""
        keys = {s["key"] for s in reversal.signs}
        fresh = [s for s in reversal.signs if s["key"] not in self._seen.get(ticket, set())]
        self._seen[ticket] = keys  # a sign the market took back is announced again if it returns
        forming: list[dict] = []
        if reversal.forming:
            mark = (reversal.forming_closes_at, tuple(s["key"] for s in reversal.forming))
            if self._forming.get(ticket) != mark:
                self._forming[ticket] = mark
                forming = reversal.forming
        return fresh, forming

    def forget(self, open_tickets: set[int]) -> None:
        for store in (self._seen, self._forming):
            for ticket in [t for t in store if t not in open_tickets]:
                del store[ticket]
