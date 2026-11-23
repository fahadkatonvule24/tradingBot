"""Move an open trade's stop to breakeven at 1R, then trail it, for every position on the account.

The bot's own trades get this from the engine. Trades placed by hand (on the phone) got nothing:
on 17 Nov 2026 a gold sell went 1.4R into profit, came all the way back through a US news release
and lost the full stop, $44.67, where breakeven at +1R would have closed it at about +$4.

1R is the distance from the entry to the trade's *first* stop: the stop on the opening order, or
the first stop the dashboard saw (a phone trade often gets its stop a moment after it opened). A
trade that never had a stop has no 1R and is left alone. Once the price where the trade would
close (the bid for a buy, the ask for a sell) is ``management.breakeven_r`` R in profit, the stop
moves to the entry plus ``breakeven_buffer_r`` R. A stop is only ever moved closer to the price,
never away from it, and never inside the broker's minimum distance.

Trailing (``management.auto_trailing``): once the last *closed* bar's close is ``trail_start_r`` R
in profit, the stop follows ``trail_atr`` ATR behind that close, as the engine does for the bot's
own trades, so it moves once a bar and only ever tighter. It needs the bar's ATR, so it applies to
the instruments on the dashboard; a trade on another instrument gets breakeven only. Measured on
the bot's signals (``scripts/research_trailing.py``, 2026-11-19): with the 2R target this trail
made no difference to the result (within 0.01R of breakeven alone, both periods); tighter trails
(1 to 1.5 ATR from 1R) did worse, so it is not tightened further.

Three more, asked for by the user on 2026-11-19 for trades placed by hand, each with its switch:

  auto_stop          a trade with no stop for ``STOP_GRACE_SECONDS`` gets one at ``risk.sl_atr`` ATR
                     from its entry (from the price, if price is already past that point)
  auto_reverse_exit  close the whole trade when the reversal check (``tradebot.reversal``) confirms a
                     sign against it on a closed bar, if it is in profit at that close and at the
                     live price. Measured (``scripts/research_reversal_exit.py``): better on 13 of 16
                     setup/timeframe runs (all four setups on H1), win rate up on all 16; it
                     held on all four timeframes for EMA cross and retest, not for breakout
                     (M30 worse) or pullback (M15, M30 worse)
  auto_partial       close ``partial_tp_fraction`` of the trade once a closed bar is ``partial_tp_r``
                     R in profit (the bot's own rule; a small gain in both test periods). A 0.01
                     lot trade cannot be halved and is left whole

At most one action per trade per refresh, in that order, then breakeven or the trail.

The reversal exit, half off and the trail read the last closed bar, so they wait for a bar that
closed after the trade opened: a bar from before the entry says nothing about the trade (found in
the audit of 19 Nov 2026: a trade opened at -0.2R was "half closed at +2R" from the bar before it).
Half off also needs the live price to be ``partial_tp_r`` R in profit, and reports that R.

This changes the account, so each part has its own switch, and it needs algo trading allowed in
the terminal; on a real-money account ``allow_real_account`` too. It never opens a trade, and
``dry_run`` and the order gate (which are about the bot's own entries) are not involved.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import pandas as pd

from tradebot.broker.base import Account, Position, SymbolSpec
from tradebot.config import BotConfig, ManagementParams, RiskParams

log = logging.getLogger(__name__)

STOP_GRACE_SECONDS = 120
"""A trade placed by hand often gets its stop a moment after it opens: it is left alone this long."""
RETRY_SECONDS = 60.0
"""After a refused change, the same trade is tried again this much later, not every second."""


@dataclass(frozen=True)
class Gate:
    on: bool
    reason: str


class Bar(NamedTuple):
    """An instrument's last closed bar, as management reads it."""

    close: float
    atr: float
    closed: datetime | None = None
    """When the bar closed (UTC). Without it, nothing is decided from the bar's close."""


def last_bar(frame: pd.DataFrame, bar_minutes: int) -> Bar | None:
    """The last closed bar of a feature frame (``time`` is its open, UTC), or None."""
    if not len(frame) or "atr" not in frame:
        return None
    opened = pd.Timestamp(frame["time"].iat[-1])
    closed = (opened + timedelta(minutes=bar_minutes)).to_pydatetime()
    return Bar(float(frame["close"].iat[-1]), float(frame["atr"].iat[-1]), closed)


def breakeven_gate(config: BotConfig, account: Account, terminal_allows: bool) -> Gate:
    """May stops be moved on this account right now (breakeven or trailing), and if not, why not."""
    m = config.management
    if not (m.auto_breakeven or m.auto_trailing or m.auto_partial or m.auto_reverse_exit or m.auto_stop):
        return Gate(
            False,
            "management.auto_breakeven, auto_trailing, auto_partial, auto_reverse_exit "
            "and auto_stop are all off in the config",
        )
    if account.is_real and config.allow_real_account is not True:
        return Gate(False, "this is a REAL-money account and allow_real_account is false")
    if not terminal_allows:
        return Gate(False, "algo trading is disabled in the MT5 terminal (the Algo Trading button)")
    return Gate(True, "on")


@dataclass(frozen=True)
class Move:
    position: Position
    stop: float
    """The new stop: the entry plus the buffer."""
    profit_r: float
    """How far the trade was in profit, in R, when the move was decided."""
    risk: float
    """1R as a price distance."""
    kind: str = "breakeven"
    """"breakeven", "trail", "stop" (a stop added from the entry) or "stop_from_price"."""
    atr: float = 0.0
    """The ATR the trailing stop was set from."""


def breakeven_move(
    position: Position, spec: SymbolSpec, bid: float, ask: float, m: ManagementParams
) -> Move | None:
    """The breakeven stop for ``position`` if it should be moved now, else None."""
    first, side = position.initial_sl, position.side
    if not first or side not in (1, -1):
        return None  # no stop, no 1R
    risk = (position.price_open - first) * side
    if not risk > 0:
        return None  # the first stop was not on the losing side
    price = bid if side > 0 else ask  # where the trade would close now
    profit_r = (price - position.price_open) * side / risk
    if profit_r < m.breakeven_r:
        return None
    stop = spec.round_price(position.price_open + side * m.breakeven_buffer_r * risk)
    if position.sl and (stop - position.sl) * side < spec.point / 2:
        return None  # the stop is already there, or better
    gap = spec.stops_level * spec.point
    if (side > 0 and not stop < bid - gap) or (side < 0 and not stop > ask + gap):
        return None  # too close to the price for the broker to accept
    return Move(position, stop, profit_r, risk)


@dataclass(frozen=True)
class Close:
    """Close all or part of a trade."""

    position: Position
    volume: float
    """Lots to close: the whole position for "reversal"."""
    kind: str
    """"partial" or "reversal"."""
    profit_r: float | None
    """How far the trade was in profit, in R (None: no stop, no R)."""
    reason: str = ""


def stop_for(
    position: Position, spec: SymbolSpec, atr: float, bid: float, ask: float, risk: RiskParams, now: datetime
) -> Move | None:
    """A stop for a trade that has none: ``sl_atr`` ATR from the entry, or from the price when
    price is already past that point. None if it has a stop or is too new to judge."""
    side = position.side
    if position.sl or side not in (1, -1) or not atr > 0:
        return None
    if (now - position.time_open).total_seconds() < STOP_GRACE_SECONDS:
        return None
    distance = risk.sl_atr * atr
    gap = spec.stops_level * spec.point
    fits = lambda stop: stop < bid - gap if side > 0 else stop > ask + gap  # noqa: E731
    stop, kind = spec.round_price(position.price_open - side * distance), "stop"
    if not fits(stop):  # price is already beyond 1R from the entry: limit what is left
        price = bid if side > 0 else ask
        stop, kind = spec.round_price(price - side * distance), "stop_from_price"
        if not fits(stop):
            return None
    price = bid if side > 0 else ask
    return Move(position, stop, (price - position.price_open) * side / distance, distance, kind, atr)


def reversal_close(
    position: Position, close: float, bid: float, ask: float, level: str, why: str = ""
) -> Close | None:
    """Close the whole trade if the reversal check stands against it and it is in profit both at
    the last close and at the live price."""
    side = position.side
    if level not in ("warn", "exit") or side not in (1, -1):
        return None
    price = bid if side > 0 else ask
    if (close - position.price_open) * side <= 0 or (price - position.price_open) * side <= 0:
        return None
    risk = _first_risk(position)
    return Close(
        position,
        position.volume,
        "reversal",
        (price - position.price_open) * side / risk if risk else None,
        why,
    )


def partial_close(
    position: Position, spec: SymbolSpec, close: float, m: ManagementParams,
    bid: float | None = None, ask: float | None = None,
) -> Close | None:  # fmt: skip
    """Close ``partial_tp_fraction`` of the trade once the last close is ``partial_tp_r`` R in
    profit and, given ``bid`` and ``ask``, the price it would close at now still is."""
    risk = _first_risk(position)
    if risk is None or position.partially_closed:
        return None
    profit_r = (close - position.price_open) * position.side / risk
    if profit_r < m.partial_tp_r:
        return None
    if bid is not None and ask is not None:
        price = bid if position.side > 0 else ask
        profit_r = (price - position.price_open) * position.side / risk
        if profit_r < m.partial_tp_r:
            return None  # it has come back since that close: what would be banked is less
    volume = spec.normalize_volume(position.volume * m.partial_tp_fraction)
    if volume < spec.volume_min or position.volume - volume < spec.volume_min - 1e-9:
        return None  # 0.01 lot: nothing to split
    return Close(position, volume, "partial", profit_r)


def _first_risk(position: Position) -> float | None:
    first, side = position.initial_sl, position.side
    if not first or side not in (1, -1):
        return None
    risk = (position.price_open - first) * side
    return risk if risk > 0 else None


def trailing_move(
    position: Position,
    spec: SymbolSpec,
    close: float,
    atr: float,
    bid: float,
    ask: float,
    m: ManagementParams,
) -> Move | None:
    """The trailing stop for ``position`` from the last closed bar (``close``, ``atr``), if it
    tightens the stop now, else None."""
    risk, side = _first_risk(position), position.side
    if risk is None or not (atr > 0 and close > 0):
        return None
    profit_r = (close - position.price_open) * side / risk
    if profit_r < m.trail_start_r:
        return None
    stop = spec.round_price(close - side * m.trail_atr * atr)
    if position.sl and (stop - position.sl) * side < spec.point / 2:
        return None  # the stop is already there, or better
    if (stop - position.price_open) * side < m.breakeven_buffer_r * risk:
        return None  # behind breakeven: that is breakeven's job
    gap = spec.stops_level * spec.point
    if (side > 0 and not stop < bid - gap) or (side < 0 and not stop > ask + gap):
        return None
    return Move(position, stop, profit_r, risk, "trail", atr)


class BreakevenKeeper:
    """Applies ``breakeven_move`` and ``trailing_move`` to every open position on the terminal,
    sending the tighter of the two when both are due."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._refused: dict[int, float] = {}
        """Ticket -> when the broker last refused its move: retried after ``RETRY_SECONDS``."""
        self.moved = 0

    def run(
        self, broker: Any, positions: list[Position], config: BotConfig,
        bars: Mapping[str, tuple[Any, ...]] | None = None,
        turned: dict[int, tuple[str, str]] | None = None, now: datetime | None = None,
        bots: bool = False,
    ) -> list[tuple[Move | Close, Any]]:  # fmt: skip
        """Do what is due. ``bars`` holds each instrument's last closed bar (``Bar``: close, ATR
        and when it closed; one that closed before a trade opened only sizes its stop); ``turned``
        each ticket's confirmed reversal level and why; ``now`` the UTC time, for the stop grace
        period; ``bots`` True to manage the bot's own trades too (no live trading loop runs to do
        it). Returns (action, broker result) for every change sent."""
        clock_now = self._clock()
        out = []
        open_tickets = {p.ticket for p in positions}
        for ticket in [t for t in self._refused if t not in open_tickets]:
            del self._refused[ticket]
        for position in positions:
            if position.magic == config.magic and not bots:
                continue  # the bot's own trade: the trading loop manages it, and two managers would each close half
            refused = self._refused.get(position.ticket)
            if refused is not None and clock_now - refused < RETRY_SECONDS:
                continue
            try:
                spec, tick = broker.symbol(position.symbol), broker.tick(position.symbol)
            except Exception:
                log.debug("%s: no quote to judge the stop on", position.symbol, exc_info=True)
                continue
            params = config.for_symbol(position.symbol)
            m = params.management
            raw = (bars or {}).get(position.symbol)
            bar = Bar(*raw) if raw else None
            # a bar that closed before the trade opened: its prices are not the trade's
            since = bar is not None and bar.closed is not None and bar.closed > position.time_open
            action: Move | Close | None = None
            if m.auto_stop and not position.sl and bar and now is not None:
                action = stop_for(position, spec, bar.atr, tick.bid, tick.ask, params.risk, now)
            if (
                action is None
                and m.auto_reverse_exit
                and bar
                and since
                and turned
                and position.ticket in turned
            ):
                level, why = turned[position.ticket]
                action = reversal_close(position, bar.close, tick.bid, tick.ask, level, why)
            if action is None and m.auto_partial and bar and since:
                action = partial_close(position, spec, bar.close, m, tick.bid, tick.ask)
            if action is None:
                candidates: list[Move | None] = []
                if m.auto_breakeven:
                    candidates.append(breakeven_move(position, spec, tick.bid, tick.ask, m))
                if m.auto_trailing and bar and since:
                    candidates.append(
                        trailing_move(position, spec, bar.close, bar.atr, tick.bid, tick.ask, m)
                    )
                due = [d for d in candidates if d is not None]
                if due:
                    action = max(due, key=lambda d: d.stop * position.side)  # the tighter one
            if action is None:
                continue
            if isinstance(action, Close):
                whole = action.kind == "reversal"
                result = broker.close(position, None if whole else action.volume, position.magic,
                                      "tradebot reversal" if whole else "tradebot half off")  # fmt: skip
            else:
                result = broker.modify(position, action.stop, position.tp, position.magic)
            if result.ok:
                self._refused.pop(position.ticket, None)
                self.moved += 1
            else:
                self._refused[position.ticket] = clock_now
            out.append((action, result))
        return out


def move_text(move: Move | Close, spec: SymbolSpec, ok: bool, message: str = "") -> str:
    """The action in words, for the activity feed and the phone."""
    p, d = move.position, spec.digits
    word = "BUY" if p.side > 0 else "SELL"
    trade = f"{word} {p.volume:g} {p.symbol} from {p.price_open:.{d}f}"
    if isinstance(move, Close):
        r = f" at {move.profit_r:+.1f}R" if move.profit_r is not None else ""
        if not ok:
            what = "close" if move.kind == "reversal" else f"close {move.volume:g} lots of"
            return f"Could not {what} your {trade}: {message or 'refused by the broker'}."
        if move.kind == "partial":
            return (f"Half off: closed {move.volume:g} of {p.volume:g} lots of your {trade}{r}. The rest runs on "
                    "with its stop and target.")  # fmt: skip
        return (f"Closed your {trade}{r}, in profit: the market turned against it ({move.reason or 'a reversal sign'}). "
                "Taking the profit before it goes back.")  # fmt: skip
    if move.kind in ("stop", "stop_from_price"):
        if not ok:
            return f"Could not add a stop to your {trade}: {message or 'refused by the broker'}."
        atr = move.risk / move.atr if move.atr else 0.0  # risk.sl_atr, which can differ per instrument
        where = (
            "the entry"
            if move.kind == "stop"
            else f"the current price (price was already more than {atr:g} ATR against the entry)"
        )
        return (f"Your {trade} had no stop. Stop added at {move.stop:.{d}f}, {atr:.1f} ATR from "
                f"{where}: the loss on this trade is now limited.")  # fmt: skip
    where = "to breakeven" if move.kind == "breakeven" else "(trailing)"
    if not ok:
        return f"Could not move the stop of your {trade} {where}: {message or 'refused by the broker'}."
    old = f"{p.sl:.{d}f}" if p.sl else "none"
    locked = (move.stop - p.price_open) * p.side / move.risk  # the buffer, from the config
    if move.kind == "trail":
        return (
            f"Stop trailed on your {trade}: {old} -> {move.stop:.{d}f}, "
            f"{abs(move.stop - (p.price_open + p.side * move.profit_r * move.risk)) / move.atr:.1f} ATR behind the "
            f"last close. The trade was {move.profit_r:+.1f}R in profit; if price comes back, it now closes at "
            f"about {locked:+.1f}R."
        )
    return (
        f"Stop moved to breakeven on your {trade}: {old} -> {move.stop:.{d}f}. The trade was "
        f"{move.profit_r:+.1f}R in profit; if price comes back, it now closes at about {locked:+.1f}R "
        "instead of the full loss."
    )


TITLES = {
    "breakeven": "BREAKEVEN",
    "trail": "STOP TRAILED",
    "stop": "STOP ADDED",
    "stop_from_price": "STOP ADDED",
    "partial": "HALF CLOSED",
    "reversal": "CLOSED IN PROFIT",
}


@dataclass(frozen=True)
class Done:
    """One change sent to the broker, in words, for the log, the activity feed and the phone."""

    symbol: str
    ok: bool
    closing: bool
    """It closed all or part of a trade (rather than moving a stop)."""
    title: str
    text: str


class Manager:
    """``BreakevenKeeper`` with the wording, shared by the dashboard and the trading loop: whichever
    of them holds the duty (``tradebot.duty``) runs it. A refusal is reported once per trade, not
    at every retry; every attempt is logged."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self.keeper = BreakevenKeeper(clock)
        self._warned: set[int] = set()

    @property
    def moved(self) -> int:
        return self.keeper.moved

    def run(
        self, broker: Any, positions: list[Position], config: BotConfig,
        bars: Mapping[str, tuple[Any, ...]] | None = None,
        turned: dict[int, tuple[str, str]] | None = None, now: datetime | None = None,
        bots: bool = False,
    ) -> list[Done]:  # fmt: skip
        out = []
        for move, result in self.keeper.run(broker, positions, config, bars, turned, now, bots):
            p = move.position
            text = move_text(move, broker.symbol(p.symbol), result.ok, result.message)
            if result.ok:
                log.info("%s: %s", p.symbol, text)
                self._warned.discard(p.ticket)
            else:
                log.warning("%s: %s", p.symbol, text)
                if p.ticket in self._warned:
                    continue  # said once already
                self._warned.add(p.ticket)
            closing = isinstance(move, Close)
            title = TITLES[move.kind] if result.ok else ("NOT CLOSED" if closing else "STOP NOT MOVED")
            out.append(Done(p.symbol, result.ok, closing, title, text))
        return out
