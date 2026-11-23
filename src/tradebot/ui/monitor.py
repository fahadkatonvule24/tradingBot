"""The dashboard's worker: owns the MT5 connection and keeps a JSON-ready snapshot.

Every MT5 call happens on this one thread (the MetaTrader5 package is not built for concurrent
use). Each cycle reads ticks, the account and positions; signals are recomputed only when a new
bar has closed. Orders placed from the page are queued here and executed between cycles, after
re-checking the advice and the trading gate at that moment, never trusting what the browser sent.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradebot import __version__, analysis, market, protect, smc, structure
from tradebot.advisor import (
    WORD,
    Advice,
    advise,
    next_trigger,
    pip_size,
    recent_signals,
    trade_plan,
    trend_side,
    watch_side,
)
from tradebot.book import REFUSAL, Book, BookBroker, BookError
from tradebot.broker.base import Broker, OrderRequest, SymbolSpec, Tick
from tradebot.config import STRATEGIES, BotConfig, SymbolParams, session_hours
from tradebot.duty import Duty
from tradebot.engine import Engine, Rejection
from tradebot.journal import Journal, JournalEvent
from tradebot.live import (
    BAR_COLUMNS,
    Gate,
    Link,
    fetch_frames,
    preview_features,
    resolve_symbols,
    trading_gate,
)
from tradebot.plan import SETUP_LABELS, setup_text
from tradebot.reversal import EXIT, Alerts, Reversal, Watch, assess, holding_text, watch_position
from tradebot.strategy import BLOCK_LABELS, compute_signals, explain
from tradebot.trend import TrendAlerts, describe
from tradebot.ui.desk import Desk, calc_request, market_view
from tradebot.ui.track import NoRecord, TrackRecords

log = logging.getLogger(__name__)

SPARK_BARS = 48
CHART_BARS = 600
"""Bars sent to the chart: it shows the newest 120 and scrolls back through the rest."""
SPEC_REFRESH = timedelta(minutes=5)
PREVIEW_SECONDS = 3.0
LOST_LINK = "Lost the connection to MetaTrader 5. Is the terminal open? Prices are frozen; retrying."


SWITCHABLE = ("M5", "M15", "M30", "H1", "H4")
CONFIGURED = "configured"
"""The setup switch's "back to the setups in the config", each symbol's own included."""


QUEUED, RUNNING, CANCELLED = "queued", "running", "cancelled"
"""Where a request to the MT5 thread is: one that timed out while still queued is cancelled and
never runs; one already running is reported as pending, never as a failure."""


@dataclass
class _Switch:
    timeframe: str = ""
    strategy: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] = field(default_factory=dict)
    state: str = QUEUED


@dataclass
class _Order:
    symbol: str
    side: int
    manual: bool = False
    override: bool = False
    """"Get in": over the bot's limits, as your own call."""
    duplicate: int | None = None
    """"Duplicate": the ticket of the bot's trade to copy (``symbol`` and ``side`` unused)."""
    request_id: str = ""
    """The page's id for this confirmation: the same id sent again never places a second order."""
    volume: float | None = None
    """"Your size": the lots you typed, instead of the bot's size (manual only)."""
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] = field(default_factory=dict)
    state: str = QUEUED


OWN_SIZE_MAX_RISK = 20.0
"""The most of equity, in percent, that one "Your size" order may lose at its stop. The bot's own
limit per trade is set aside for it (that is the point), but a slip of the finger (0.2 lots for
0.02) must not send ten times the trade meant."""


@dataclass
class _Task:
    """Something a web or Telegram thread wants done on the MT5 thread."""

    fn: Any
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    state: str = QUEUED


class Monitor:
    def __init__(
        self,
        config: BotConfig,
        broker: Broker,
        symbols: list[str],
        *,
        live_flag: bool = False,
        journal_path: str | Path | None = None,
        interval: float = 1.0,
        notifier: Any = None,
        book: Book | None = None,
        duty: Duty | None = None,
    ) -> None:
        if book is not None and not isinstance(broker, BookBroker):
            broker = BookBroker(broker, book, config.magic)  # your own accounts, when one is selected
        self.config, self.broker, self.symbols = config, broker, symbols
        # the setups as configured, restored when the dashboard switches back to the config's own
        self._configured_setup, self._configured_overrides = config.strategy.name, config.overrides
        self._configured_mixed = any(n != config.strategy.name for n in config.setups().values())
        self.currency = "USD"
        self.market_cache: tuple[Any, dict[str, Any]] | None = None
        self.levels_cache: dict[str, tuple[float, Any]] = {}
        self.daily_cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._swaps: dict[str, tuple[float, Any]] = {}
        self.news_seen: set[tuple[Any, ...]] = set()
        self.news_feed: Any = None
        """The background download of the week's calendar (``tradebot ui`` sets it), for its status."""
        self.live_flag = live_flag
        self.interval = interval
        self.engine = Engine(config, broker, execute=False, live=True)  # guards and sizing only
        self.track_record: dict[str, Any] = {}
        self._journal_path = journal_path
        self._journal: Journal | None = None
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"ready": False, "version": __version__}
        self._features: dict[str, pd.DataFrame] = {}
        self._fetched: dict[str, float] = {}
        self._recent: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._link = Link(broker)
        self._last_error: str | None = None
        self._symbol_errors: dict[str, str] = {}
        self.notifier = notifier
        self._previews: dict[str, tuple[float, pd.DataFrame | None]] = {}
        self._track_args: tuple[Any, Any] | None = None
        self.tracks = TrackRecords()
        self._heartbeat_day: Any = None
        self._manual_seen: set[int] | None = None
        self._weekly_sent: tuple | None = None
        self.heartbeat = False  # `tradebot ui` turns it on: STARTED, then RUNNING each trading day
        self._track_pending = False
        self._htf: dict[str, dict[str, pd.DataFrame]] = {}
        self._forming_seen: set[tuple[Any, ...]] = set()
        self._watches: dict[int, tuple[Any, Watch]] = {}
        self._reversal_alerts = Alerts()
        self._others: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._engines: dict[str, Engine] = {}
        self._signal_seen: set[tuple[Any, ...]] = set()
        self._breakeven = protect.Manager()
        self.duty = duty
        """With ``tradebot ui``: whether this dashboard manages trades placed by hand and sends the
        reversal warnings, or leaves both to the trading loop (``tradebot.duty``). None: always."""
        self._turned: dict[int, tuple[Any, tuple[str, str]]] = {}
        self._breakeven_state: dict[str, Any] = {"on": False, "reason": "not checked yet", "moved": 0}
        self._trends: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._structures: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._trend_alerts = TrendAlerts()
        self._specs: dict[str, tuple[SymbolSpec, datetime]] = {}
        self._account_id: Any = None
        self._last_action: dict[str, str] = {}
        self._analysis_seen: dict[str, tuple[Any, ...]] = {}
        self._analysis_changes: dict[str, dict[str, Any]] = {}
        self._orders: queue.Queue[_Order | _Switch | _Task] = queue.Queue()
        self._queue_lock = threading.Lock()
        self._requests: OrderedDict[str, _Order] = OrderedDict()
        """Recent orders by the page's request id, so a repeated request is answered, not re-sent."""
        self._stop = threading.Event()
        self.activity: deque[dict[str, Any]] = deque(maxlen=50)
        self.desk = Desk(self, book) if book is not None else None

    # --- read side (web server threads) -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._state

    def call(self, fn: Any, timeout: float = 20.0) -> Any:
        """Run ``fn`` on the MT5 thread, between refreshes, and hand back what it returns."""
        task = _Task(fn)
        self._orders.put(task)
        if not self._wait(task, timeout):
            return self._late(task, "")
        return task.result

    def _wait(self, item: _Order | _Switch | _Task, timeout: float) -> bool:
        """Wait for the MT5 thread to finish ``item``. On a timeout, an item it has not started is
        cancelled, so it never runs after its caller was told it failed. False on a timeout."""
        if item.done.wait(timeout):
            return True
        with self._queue_lock:
            if item.state == QUEUED:
                item.state = CANCELLED
        return item.done.is_set()

    @staticmethod
    def _late(item: _Order | _Switch | _Task, what: str) -> dict[str, Any]:
        """The answer for an item the MT5 thread did not finish in time."""
        if item.state == CANCELLED:
            done = f"{what} was not sent" if what else "nothing was done"
            return {"ok": False, "message": f"timed out waiting for the MT5 thread: {done}"}
        return {
            "ok": False,
            "pending": True,
            "message": f"still in progress on the MT5 thread{': ' + what + ' may yet be sent' if what else ''}. "
            "Check again before trying it a second time.",
        }

    def _claim(self, item: _Order | _Switch | _Task) -> bool:
        """The worker takes ``item``: False if its caller already gave up on it (cancelled)."""
        with self._queue_lock:
            if item.state == CANCELLED:
                return False
            item.state = RUNNING
            return True

    def calc(self, body: dict[str, Any]) -> dict[str, Any]:
        """The position-size, margin and leverage calculator, on the MT5 thread."""

        def run() -> dict[str, Any]:
            try:
                return {"ok": True, "result": clean(calc_request(self, body))}
            except (BookError, ValueError, KeyError) as exc:
                return {"ok": False, "message": str(exc)}

        return self.call(run)

    def book_op(self, body: dict[str, Any]) -> dict[str, Any]:
        """A change to your accounts or logged trades, requested by the page."""
        if self.desk is None:
            return {"ok": False, "message": "the account book is not available in this session"}
        return self.call(lambda: self.desk.op(body))

    def command(self, text: str) -> str:
        """A command typed on the phone (Telegram). Returns the reply, or "" for anything else."""
        words = text.strip().split()
        if words and words[0].split("@")[0].lower() in ("/analysis", "/analyse"):
            if len(words) != 2:
                return "Use: /analysis SYMBOL"
            reply = self.call(lambda: self.analysis_text(self.match_symbol(words[1])), timeout=30.0)
            return reply if isinstance(reply, str) else str(reply.get("message", ""))
        if self.desk is None:
            return ""
        reply = self.call(lambda: self.desk.command(text), timeout=30.0)
        return reply if isinstance(reply, str) else str(reply.get("message", ""))

    # --- helpers for the desk (MT5 thread) ----------------------------------------------------

    def features(self, symbol: str) -> pd.DataFrame | None:
        return self._features.get(symbol)

    def spec(self, symbol: str) -> SymbolSpec | None:
        cached = self._specs.get(symbol)
        return cached[0] if cached else None

    def htf(self, symbol: str) -> dict[str, pd.DataFrame]:
        return self._htf.get(symbol) or {}

    def swap(self, symbol: str) -> Any:
        """Overnight financing per lot at the terminal's broker, read every few minutes."""
        cached = self._swaps.get(symbol)
        if cached is None or time.monotonic() - cached[0] > SPEC_REFRESH.total_seconds():
            read = getattr(self.broker, "swap", None)
            try:
                value = read(symbol) if read else None
            except Exception:
                log.debug("%s: swap not read", symbol, exc_info=True)
                value = None
            cached = (time.monotonic(), value)
            self._swaps[symbol] = cached
        return cached[1]

    def match_symbol(self, text: str) -> str:
        """A watched symbol from what a person typed: EURUSD finds EURUSDm."""
        wanted = str(text).strip().upper()
        for exact in (True, False):
            for symbol in self.symbols:
                if wanted and (symbol.upper() == wanted if exact else symbol.upper().startswith(wanted)):
                    return symbol
        raise ValueError(f"{text!r} is not on the watchlist ({', '.join(self.symbols)})")

    def advice_now(self, symbol: str) -> Advice:
        """Fresh advice at the live quote, for whichever account the broker is answering for."""
        now = self.broker.now()
        engine = Engine(self.config, self.broker, execute=False, live=True)
        return advise(
            engine, symbol, self.broker.symbol(symbol), self.config.for_symbol(symbol),
            self._features_for(symbol, now), self.broker.tick(symbol), now,
        )  # fmt: skip

    def book_changed(self) -> None:
        """The selected account or its trades changed: drop what was worked out from the old ones."""
        self.engine = Engine(self.config, self.broker, execute=False, live=True)
        for cache in (self._engines, self._watches, self._specs):
            cache.clear()

    def note_book(self, symbol: str, action: str, text: str, send: bool = False) -> None:
        self.activity.append({"time": self.broker.now(), "symbol": symbol, "action": action, "text": text})
        log.info("%s %s", symbol, text)
        if send:
            self._send(f"{action} · {symbol}\n{text}", "position")

    def status_text(self) -> str:
        r, a = self.engine.risk_status(self.broker.now()), self.broker.account()
        name = (self.desk.broker.account_name() if self.desk else None) or "MetaTrader 5 account"
        stop = lambda limit: f"stop at -{limit:g}%" if limit > 0 else "no limit"  # noqa: E731
        entries = f"{r.entries_today}/{r.entries_limit}" if r.entries_limit else str(r.entries_today)
        return "\n".join([
            f"{name}: equity {a.equity:,.2f} {a.currency}",
            f"Today {r.day_percent:+.2f}% ({stop(r.daily_limit)}) · this week {r.week_percent:+.2f}% "
            f"({stop(r.weekly_limit)})",
            f"Open risk {f'{r.open_risk_percent:.2f}%' if r.open_risk_percent is not None else 'unknown'} "
            f"of {r.open_risk_limit:g}% · {r.positions}/{r.positions_limit} "
            f"positions · {r.losing_streak} losses in a row · {entries} entries today",
            f"New entries are stopped: {r.blocked}" if r.blocked else "No limit is hit.",
        ])  # fmt: skip

    def analysis_text(self, symbol: str) -> str:
        """Read-only assessment, rechecking freshness when a phone requests it."""
        state = self.snapshot()
        if state.get("error"):
            return f"{symbol}: analysis unavailable. {state['error']}"
        row = next((r for r in state.get("symbols", []) if r["symbol"] == symbol), None)
        if row is None or row.get("error"):
            return f"{symbol}: analysis is not available yet."
        context = (state.get("market") or {}).get("symbols", {}).get(symbol, {})
        found = self._analysis_for(row, context, self.broker.now())
        source = (state.get("account") or {}).get("server") or "connected price feed"
        return f"{analysis.format_text(symbol, found)}\nSource: {source}."

    def plan_text(self, symbol: str, side: int) -> str:
        """The priced plan for one side, with the size at each of your accounts."""
        advice = self.advice_now(symbol)
        plan = advice.plans.get("buy" if side > 0 else "sell")
        if plan is None:
            return f"{symbol}: {advice.headline}. {advice.detail}"
        spec, held = self.broker.symbol(symbol), self.broker.positions(None)
        if self.desk:
            row = self._features_for(symbol, self.broker.now()).iloc[-1]
            self.desk.sizes(symbol, self.config.for_symbol(symbol), row, self.broker.tick(symbol))
        signal = advice.actionable and advice.side == side
        shown = replace(advice, action=WORD[side], headline="The bot is signalling this" if signal
                        else "Your call, not a bot signal", detail="Priced at the live quote")  # fmt: skip
        lines = self._alert_text(symbol, shown, spec, plan).split("\n")
        if not plan.volume and len(lines) == 2:
            lines.append(f"No size: {plan.rejected}")
        if advice.blocker:
            lines.append(f"Entries are paused: {advice.blocker}")
        specs = {s: self.broker.symbol(s) for s in {symbol, *(p.symbol for p in held)}}
        lines.append(market.overlap(symbol, side, specs, held))
        return "\n".join(line for line in lines if line)

    def market_text(self) -> str:
        view = self.snapshot().get("market")
        if not view:
            return "The market view is not ready yet."
        lines = [
            "Sessions: "
            + ", ".join(f"{s['name']} {'open' if s['open'] else 'closed'}" for s in view["sessions"])
        ]
        if view["strength"]:
            lines.append("Strength, last day (last week), in ATR-scaled units:")
            lines += [
                f"  {r['asset']} {r['day']:+.1f}" + ("" if r["week"] is None else f" ({r['week']:+.1f})")
                for r in view["strength"]
            ]
        if view["exposure"]:
            lines.append("Your exposure:")
            lines += [
                f"  {'long' if r['amount'] > 0 else 'short'} {r['asset']} {abs(r['amount']):,.0f} {self.currency}"
                + ("" if r["percent"] is None else f" ({abs(r['percent']):.0f}% of equity)")
                for r in view["exposure"]
            ]
        else:
            lines.append("No open positions, so no exposure.")
        return "\n".join([*lines, *view["warnings"]])

    def levels_text(self, symbol: str) -> str:
        view = (self.snapshot().get("market") or {}).get("symbols", {}).get(symbol) or {}
        found, spec = view.get("levels"), self.broker.symbol(symbol)
        if not found:
            return f"{symbol}: no daily levels yet."
        d, pip = spec.digits, pip_size(spec)
        lines = [f"{symbol} levels, from the top:"]
        lines += [f"  {r['name']} {r['price']:.{d}f} ({r['away_atr']:+.1f} ATR)" for r in found["levels"]]
        if found["range_used"] is not None:
            lines.append(
                f"Today's range is {found['day_range'] / pip:.0f} {'pips' if d in (3, 5) else 'points'}: "
                f"{found['range_used']:.0f}% of the {found['adr_days']}-day average."
            )
        return "\n".join(lines)

    def set_timeframe(self, timeframe: str, timeout: float = 30.0) -> dict[str, Any]:
        """Switch the trading timeframe (handled on the MT5 thread, between refreshes)."""
        switch = _Switch(str(timeframe).upper())
        self._orders.put(switch)
        if not self._wait(switch, timeout):
            return self._late(switch, "")
        return switch.result

    def set_strategy(self, name: str, timeout: float = 30.0) -> dict[str, Any]:
        """Switch the setup the advisor watches (handled on the MT5 thread, between refreshes)."""
        switch = _Switch(strategy=str(name).lower())
        self._orders.put(switch)
        if not self._wait(switch, timeout):
            return self._late(switch, "")
        return switch.result

    def _reconfigure(self, config: BotConfig) -> str | None:
        """Swap in a changed config and drop everything computed from the old one."""
        try:
            config.validate()
        except ValueError as exc:
            return str(exc)
        self.config = config
        self.engine = Engine(config, self.broker, execute=False, live=True)
        with self._lock:
            self._features.clear()
        for cache in (
            self._fetched,
            self._recent,
            self._previews,
            self._last_action,
            self._htf,
            self._watches,
            self._others,
            self._engines,
            self._trends,
            self._structures,
            self._analysis_seen,
            self._analysis_changes,
        ):
            cache.clear()
        # _forming_seen and _signal_seen survive a switch: their keys name the setup and the
        # timeframe, so switching away and back does not announce the same trigger again.
        self.market_cache = None
        self.levels_cache.clear()
        self.daily_cache.clear()
        cached = (
            self.tracks.cached(config, *self._track_args) if self._track_args else self.tracks.cached(config)
        )
        self.track_record = cached or {}
        if self._track_args and cached is None:
            self._track_pending = True
            threading.Thread(
                target=self.load_track_record, args=self._track_args, name="track-record", daemon=True
            ).start()
        return None

    def _configured(self) -> BotConfig:
        """The current config with the setups as configured: the main one and each symbol's own."""
        return replace(self.config.with_setup(self._configured_setup), overrides=self._configured_overrides)

    def _apply_strategy(self, name: str) -> dict[str, Any]:
        """``name``: one setup for every symbol, those with their own setup in the config included
        (a tile's "show this setup" must show it even when it is the config's main setup), or
        ``CONFIGURED``: back to the config's own setups, per symbol."""
        if name == CONFIGURED:
            config = self._configured()
            message = f"Back to the setups in the config: {setup_text(config)}."
        elif name in STRATEGIES:
            config = self.config.with_setup(name)
            message = f"Now watching the {name.replace('_', ' ')} setup."
        else:
            return {"ok": False, "message": f"strategy must be one of {', '.join((*STRATEGIES, CONFIGURED))}"}
        same = config.strategy.name == self.config.strategy.name and config.setups() == self.config.setups()
        if not same or config.overrides != self.config.overrides:
            error = self._reconfigure(config)
            if error:
                return {"ok": False, "message": error}
            log.info("setup switched to %s", setup_text(config))
        return {"ok": True, "message": message, "strategy": config.strategy.name}

    def _apply_timeframe(self, timeframe: str) -> dict[str, Any]:
        if timeframe not in SWITCHABLE:
            return {"ok": False, "message": f"timeframe must be one of {', '.join(SWITCHABLE)}"}
        if timeframe != self.config.timeframe:
            error = self._reconfigure(replace(self.config, timeframe=timeframe))
            if error:
                return {"ok": False, "message": error}
            self._trend_alerts.clear()  # another timeframe's trends are other trends
            log.info("trading timeframe switched to %s", timeframe)
        ladder = self.trend_timeframes()
        trend = f"direction from {' + '.join(ladder)}" if ladder else "no trend filter"
        return {"ok": True, "message": f"Now analysing {timeframe} ({trend}).", "timeframe": timeframe}

    def trend_timeframes(self) -> list[str]:
        strategy = self.config.strategy
        return list(strategy.trend_ladder(self.config.timeframe)) if strategy.use_trend_filter else []

    def plan(self) -> dict[str, Any]:
        """The written trading plan for the setup and timeframe on screen."""
        from tradebot.plan import plan_title, trading_plan

        config = self.config
        sections = trading_plan(config, self.track_record or None, measuring=self._track_pending)
        return {
            "title": plan_title(config),
            "sections": [{"title": s.title, "lines": list(s.lines)} for s in sections],
        }

    def chart(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            features = self._features.get(symbol)
        if features is None:
            return None
        p = self.config.for_symbol(symbol).strategy
        tail = features.tail(CHART_BARS)
        bars = tail[["time", "open", "high", "low", "close", "ema_fast", "ema_slow"]].to_dict("records")
        trend = [trend_side(row, p) for _, row in tail.iterrows()]  # a running trend, bar by bar
        marks = [  # every entry trigger on screen: taken ones open a join window
            {"i": i, "side": int(row["raw_signal"]), "taken": bool(row["signal"] != 0), "price": float(row["close"]),
             "blocked_by": "" if row["signal"] != 0 else BLOCK_LABELS.get(row["block"], str(row["block"]))}
            for i, (_, row) in enumerate(tail.iterrows())
            if row["raw_signal"] != 0
        ]  # fmt: skip
        offset = len(features) - len(tail)  # structure positions are in the full frame
        found = self._structures.get(symbol)
        shape = found[1] if found and found[0] == features["time"].iloc[-1] else None
        drawn = None
        if shape:
            drawn = {
                "swings": [{**s, "i": s["i"] - offset} for s in shape["swings"] if s["i"] >= offset],
                "zones": shape["zones"],
                "round": shape["round"],
                "patterns": [
                    {"name": x["name"], "status": x["status"], "side": x["side"],
                     "lines": [_clip_line(line, offset) for line in x["lines"]]}
                    for x in shape["patterns"]
                ],
            }  # fmt: skip
        liquidity = smc.describe(features, offset, shown_from=offset)
        return clean({"symbol": symbol, "fast": p.fast_ema, "slow": p.slow_ema, "bars": bars, "trend": trend,
                      "marks": marks, "join_bars": p.join_window_bars, "structure": drawn,
                      "smc": liquidity})  # fmt: skip

    def submit(
        self, symbol: str, side: int, manual: bool = False, timeout: float = 20.0, *, override: bool = False,
        duplicate: int | None = None, request_id: str = "", volume: float | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Queue an order for the worker thread and wait for its outcome.

        An order still queued when the wait runs out is cancelled: it is never sent after the page
        was told it failed. One already being sent is reported as pending. ``request_id`` (the
        page's, per confirmation) makes a repeat of the same request wait for the first one's
        outcome instead of sending a second order."""
        with self._queue_lock:
            order = self._requests.get(request_id) if request_id else None
            if order is not None and order.state == CANCELLED:
                order = None  # it was never sent: this repeat may send it
            repeat = order is not None
            if order is None:
                order = _Order(symbol, side, manual, override, duplicate, request_id, volume)
                if request_id:
                    self._requests[request_id] = order
                    while len(self._requests) > 50:
                        self._requests.popitem(last=False)
                self._orders.put(order)
        if not self._wait(order, timeout):
            return self._late(order, "the order")
        return {**order.result, "repeat": True} if repeat else order.result

    # --- worker thread -------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if self._journal_path:
            self._journal = Journal(self._journal_path)  # sqlite objects stay on the thread that made them
        try:
            while not self._stop.is_set():
                deadline = time.monotonic() + self.interval
                self.cycle()
                while not self._stop.is_set():
                    # Requests from the page are served after every refresh, even one that overran
                    # its second: waiting for spare time starved them when refreshes got slower.
                    remaining = deadline - time.monotonic()
                    try:
                        order = (
                            self._orders.get(timeout=remaining)
                            if remaining > 0
                            else self._orders.get_nowait()
                        )
                    except queue.Empty:
                        break
                    if not self._claim(order):
                        continue  # its caller timed out and was told so: it must not run now
                    if isinstance(order, _Task):
                        try:
                            order.result = order.fn()
                        except Exception as exc:
                            log.exception("request failed")
                            order.result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
                    elif isinstance(order, _Switch):
                        order.result = (
                            self._apply_strategy(order.strategy)
                            if order.strategy
                            else self._apply_timeframe(order.timeframe)
                        )
                    else:
                        order.result = self._safe_execute(order)
                    order.done.set()
        finally:
            if self._journal:
                self._journal.close()

    def cycle(self) -> None:
        if not self._link.ok():
            return self._report(LOST_LINK)
        try:
            self.refresh()
        except Exception as exc:  # keep the page alive and say what went wrong
            if not self._link.ok():
                return self._report(LOST_LINK)
            message = f"{type(exc).__name__}: {exc}"
            if message != self._last_error:  # the same failure every second is logged once
                log.exception("dashboard refresh failed")
            self._last_error = message
            self._report(message)
        else:
            self._last_error = None
        # Stops are protected even when the analysis failed this second: a bad bar on one
        # symbol must not leave every open trade without its breakeven.
        try:
            self._keep_breakeven()
        except Exception:  # protecting stops must never stop the dashboard
            log.warning("automatic breakeven check failed", exc_info=True)

    def _keep_breakeven(self) -> None:
        """Protect the terminal's open trades, the phone's included (``tradebot.protect``): stops
        added, breakeven, the trail, half off and the reversal exit, each with its switch. Only
        while this process holds the duty (``tradebot.duty``): otherwise the trading loop does it."""
        inner = getattr(self.broker, "inner", self.broker)
        allowed = getattr(inner, "terminal_trading_allowed", lambda: False)()  # as refresh() reads it
        gate = protect.breakeven_gate(self.config, inner.account(), allowed)
        duty = self.duty is None or self.duty.held()
        m = self.config.management
        reason = gate.reason
        if gate.on and not duty and self.duty is not None:
            reason = f"managed by {self.duty.holder_text()}"
        self._breakeven_state = {"on": gate.on and duty, "reason": reason, "moved": self._breakeven.moved,
                                 "at_r": m.breakeven_r, "buffer_r": m.breakeven_buffer_r,
                                 "breakeven": m.auto_breakeven, "trailing": m.auto_trailing,
                                 "partial": m.auto_partial, "reverse_exit": m.auto_reverse_exit, "stop": m.auto_stop,
                                 "trail_start_r": m.trail_start_r, "trail_atr": m.trail_atr,
                                 "stop_atr": self.config.risk.sl_atr, "partial_r": m.partial_tp_r,
                                 "partial_fraction": m.partial_tp_fraction,
                                 "elsewhere": gate.on and not duty}  # fmt: skip
        try:
            if gate.on and duty:
                self._move_stops(inner)
        finally:
            self._breakeven_state["moved"] = self._breakeven.moved
            with self._lock:  # the page shows it at once, not a refresh later
                if self._state.get("ready"):
                    self._state["breakeven"] = dict(self._breakeven_state)

    def _move_stops(self, inner: Any) -> None:
        # The terminal's positions whichever account is selected: with one of your own accounts
        # chosen, the page shows that account's trades, but the stops to protect are the terminal's.
        terminal = getattr(self.broker, "terminal_positions", None)
        held = terminal(None) if terminal else inner.positions(None)
        bars = {}  # the last closed bar of each instrument on the dashboard, for the trailing stop
        with self._lock:
            for name, frame in self._features.items():
                bar = protect.last_bar(frame, self.config.bar_minutes)
                if bar is not None:
                    bars[name] = bar
        turned = {}  # each position's confirmed reversal level on the last closed bar, and why
        for p in held:
            found = self._confirmed_reversal(p)
            if found is not None:
                turned[p.ticket] = found
        open_tickets = {p.ticket for p in held}
        for ticket in [t for t in self._turned if t not in open_tickets]:
            del self._turned[ticket]
        # the bot's own trades, and your Get in and Duplicate orders, are the live trading loop's
        # to manage; with none running (or none known of), they get the same protection here
        bots = self.duty is not None and not self.duty.trading_loop_running()
        for done in self._breakeven.run(inner, held, self.config, bars, turned, self.broker.now(), bots):
            self.activity.append({"time": self.broker.now(), "symbol": done.symbol,
                                  "action": ("CLOSE" if done.closing else "STOP") if done.ok else "REJECTED",
                                  "text": done.text})  # fmt: skip
            self._send(f"{done.title} · {done.symbol}\n{done.text}", "order")

    def _confirmed_reversal(self, p: Any) -> tuple[str, str] | None:
        """(level, headline) of the reversal check on closed bars for a terminal position, once per
        bar, with no alerts: the page's own check (``_reversal``) reads the selected account."""
        features = self._features.get(p.symbol)
        spec = self._specs.get(p.symbol)
        if features is None or spec is None or not len(features):
            return None
        key = (features["time"].iloc[-1], p.side, p.time_open)
        cached = self._turned.get(p.ticket)
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            watch = watch_position(features, p.side, p.time_open, self.config.bar_minutes)
            strategy = self.config.for_symbol(p.symbol).strategy
            found = assess(
                features, p.side, p.time_open, strategy, self.config.timeframe, spec[0], None, watch
            )
        except Exception:
            log.debug("%s: reversal check failed", p.symbol, exc_info=True)
            return None
        value = (found.level, found.headline)
        self._turned[p.ticket] = (key, value)
        return value

    def _report(self, error: str) -> None:
        with self._lock:
            rows = []
            for row in self._state.get("symbols", []):
                found = row.get("analysis")
                if found:
                    found = {
                        **found,
                        "status": "stale",
                        "bias": 0,
                        "headline": "Analysis paused: the price feed is unavailable",
                        "scenarios": [],
                        "conflicts": [error],
                        "change": None,
                    }
                    row = {**row, "analysis": found}
                rows.append(row)
            self._state = {**self._state, "symbols": rows, "error": error}

    def refresh(self) -> None:
        now = self.broker.now()
        identity = getattr(getattr(self.broker, "inner", self.broker), "account_id", None)
        if identity is not None:
            current = identity()
            if self._account_id is not None and current != self._account_id:
                self.book_changed()  # the terminal switched accounts: limits from the new one's history
                # Invalidate in-flight results too: load_track_record compares the config's identity.
                # Clear old prices even if resolving the new watchlist fails temporarily.
                with self._lock:
                    self._features.clear()
                self.symbols = resolve_symbols(self.broker, self.config.symbols)
                if self._track_args:
                    data_dir, old_names = self._track_args
                    names = {
                        symbol: old_names.get(symbol)
                        or max(
                            (name for name in self.config.symbols if symbol.upper().startswith(name.upper())),
                            key=len,
                            default=symbol,
                        )
                        for symbol in self.symbols
                    }
                    self._track_args = (data_dir, names)
                self._reconfigure(replace(self.config))
                for cache in (
                    self._swaps,
                    self._symbol_errors,
                    self._turned,
                    self._signal_seen,
                    self._forming_seen,
                ):
                    cache.clear()
                self._manual_seen = None
                self._breakeven = protect.Manager()
                self._reversal_alerts = Alerts()
                self._trend_alerts = TrendAlerts()
            self._account_id = current
        account = self.broker.account()
        self.currency = account.currency
        manual = bool(self.desk and self.desk.manual)
        terminal_allows = getattr(self.broker, "terminal_trading_allowed", lambda: False)()
        gate = (
            Gate(False, REFUSAL)
            if manual
            else trading_gate(self.config, account, self.live_flag, terminal_allows)
        )
        rows = []
        for symbol in self.symbols:
            try:
                rows.append(self._symbol_state(symbol, now))
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                # once per symbol and failure, not every second: when the terminal switched to an
                # account whose names differ (EURUSD -> EURUSDm), this wrote 2,250 lines in 6 minutes
                if self._symbol_errors.get(symbol) != message:
                    log.warning("%s: %s", symbol, exc)
                self._symbol_errors[symbol] = message
                rows.append({"symbol": symbol, "error": message})
            else:
                self._symbol_errors.pop(symbol, None)
        if self.heartbeat:
            self._heartbeat(now)
            self._weekly(now)
        if self.desk:  # a stop or target your broker filled while nobody told the book
            self.desk.sweep({r["symbol"]: (r["bid"], r["ask"]) for r in rows if "bid" in r})
            account = self.broker.account()
        positions = []
        held = self.broker.positions(None)
        for p in held:
            price = next((row["bid"] if p.side > 0 else row["ask"]
                          for row in rows if row.get("symbol") == p.symbol and "bid" in row), None)  # fmt: skip
            r = None
            if p.initial_risk and price is not None:  # yours from the phone too, once it has a stop
                r = (price - p.price_open) * p.side / p.initial_risk
            duplicate = None  # what the Duplicate button would send, while orders are on
            if gate.execute and p.magic == self.config.magic and p.symbol in self.symbols:
                try:
                    duplicate = self.duplicate_plan(p)
                except Exception as exc:
                    duplicate = {"refused": f"{type(exc).__name__}: {exc}"}
                stop = self.config.kill_switch_file
                if stop and Path(stop).exists():
                    duplicate = {"refused": f"new entries are paused: {stop} exists"}
            positions.append(
                asdict(p)
                | {"bot": p.magic == self.config.magic, "r": r, "reversal": self._reversal(p, price, now),
                   "book": manual, "duplicate": duplicate}
            )  # fmt: skip
        self._heads_up(held, rows, now)
        open_tickets = {p.ticket for p in held}
        self._reversal_alerts.forget(open_tickets)
        for ticket in [t for t in self._watches if t not in open_tickets]:
            del self._watches[ticket]
        server = getattr(self.broker, "server_name", lambda: "")()
        market_state = self._market(rows, held, account.equity, now)
        now = self.broker.now()  # quotes may have arrived while the symbols/context were being collected
        for row in rows:
            if row.get("error"):
                continue
            context = (market_state or {}).get("symbols", {}).get(row["symbol"], {})
            row["analysis"] = self._analysis_for(row, context, now)
            self._note_analysis(row["symbol"], row["analysis"], now)
        state = {
            "ready": True,
            "version": __version__,
            "now": now,
            "strategy": self.config.strategy.name,
            "setups": self.config.setups(),
            # the config's own per-symbol setups (None when every symbol has the main one), and
            # whether they are the ones on screen: the setup bar's "As configured"
            "configured_setups": self._configured().setups() if self._configured_mixed else None,
            "as_configured": self.config.setups() == self._configured().setups(),
            "setup_text": setup_text(self.config),
            "strategies": list(STRATEGIES),
            "timeframe": self.config.timeframe,
            "timeframes": list(SWITCHABLE),
            "trend_timeframes": self.trend_timeframes(),
            "session": "{:02d}-{:02d} UTC".format(*self.config.schedule.hours(now)),
            "account": asdict(account) | {"server": server, "floating": account.equity - account.balance},
            "gate": {"execute": gate.execute, "reason": gate.reason, "live_flag": self.live_flag},
            "breakeven": dict(self._breakeven_state),
            "symbols": rows,
            "positions": positions,
            "book": self.desk.state(now) if self.desk else None,
            "market": market_state,
            "risk": self.engine.risk_status(now),
            "activity": list(self.activity)[::-1],
            "track_record": self.track_record,
            "error": None,
        }
        with self._lock:
            self._state = clean(state)

    def _analysis_for(self, row: dict[str, Any], context: dict[str, Any], now: datetime) -> dict[str, Any]:
        return analysis.assess(
            self._features.get(row["symbol"], pd.DataFrame()),
            row.get("trend"),
            row.get("setups", []),
            context,
            timeframe=self.config.timeframe,
            bar_minutes=self.config.bar_minutes,
            now=now,
            quote_time=row.get("tick_time"),
            price=row.get("bid"),
            digits=row.get("digits", 5),
            max_bar_age_bars=self.config.schedule.max_bar_age_bars,
        )

    def _note_analysis(self, symbol: str, found: dict[str, Any], now: datetime) -> None:
        """Only a changed interpretation generates an alert; first observation is quiet.

        A pause (stale or incomplete data: the weekend, a lost feed) is not an interpretation, so
        it is compared against the last ready assessment and says nothing on its own. Otherwise
        every market close and reopen sent one alert per symbol.
        """
        key = analysis.semantic_key(found)
        if key[0] != "ready":
            found["change"] = self._analysis_changes.get(symbol)
            return
        previous = self._analysis_seen.get(symbol)
        self._analysis_seen[symbol] = key
        if previous is not None and previous != key:
            text = found["headline"]
            change = {"id": f"{symbol}:{now.isoformat()}:{key}", "time": now, "text": text}
            self._analysis_changes[symbol] = change
            self.activity.append({"time": now, "symbol": symbol, "action": "ANALYSIS", "text": text})
            self._send(f"Analysis changed · {symbol}\n{analysis.format_text(symbol, found)}", "analysis")
        found["change"] = self._analysis_changes.get(symbol)

    def _market(self, rows: list[dict[str, Any]], held: list[Any], equity: float, now: datetime) -> Any:
        try:
            return market_view(self, rows, held, equity, now)
        except Exception:  # context only: it must never take the advice down with it
            log.debug("market view failed", exc_info=True)
            return None

    def _sizes(self, symbol: str, advice: Advice, features: pd.DataFrame, tick: Any) -> Any:
        """The plan's size at each of your other accounts (None without a book or a plan)."""
        if self.desk is None or advice.plan is None:
            return None
        try:
            return self.desk.sizes(symbol, self.config.for_symbol(symbol), features.iloc[-1], tick)
        except Exception:
            log.debug("%s: account sizes not computed", symbol, exc_info=True)
            return None

    def _symbol_state(self, symbol: str, now: datetime) -> dict[str, Any]:
        spec = self._spec(symbol, now)
        tick = self.broker.tick(symbol)
        features = self._features_for(symbol, now)
        preview = self._preview_for(symbol, features)
        advice = advise(
            self.engine, symbol, spec, self.config.for_symbol(symbol), features, tick, now, preview
        )
        sizes = self._sizes(symbol, advice, features, tick)  # before the alerts, which quote them
        get_in = None
        rule = get_in_rule(advice)
        if rule is not None and not (self.desk and self.desk.manual):  # the bot's entry, held back by a limit
            params = self.config.for_symbol(symbol)
            free = trade_plan(
                self.engine, symbol, spec, params, features.iloc[-1], tick, advice.side, caps=False
            )
            get_in = {"side": advice.side, "over": rule, "plan": free}
        self._note(symbol, advice, now, spec)
        self._note_forming(symbol, advice, now, spec)
        self._note_failed(symbol, advice, now, spec)
        setups = self._setups(symbol, features, advice, tick, spec, now)
        trend = self._trend_for(symbol, features, spec, now)
        last = features.iloc[-1]
        forming = getattr(self.broker, "forming_bar", None)
        return {
            "symbol": symbol,
            "digits": spec.digits,
            "pip": pip_size(spec),
            "pip_label": "pips" if spec.digits in (3, 5) else "points",
            "volume_min": spec.volume_min,
            "volume_step": spec.volume_step,
            "auto_trade": self.config.for_symbol(symbol).risk.auto_trade,
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": tick.ask - tick.bid,
            "tick_time": tick.time,
            "prev_close": float(last["close"]),
            "atr": float(last["atr"]),
            "spark": [*features["close"].tail(SPARK_BARS).round(spec.digits).tolist(), tick.bid],
            "forming": forming(symbol, self.config.timeframe) if forming else None,
            "advice": advice,
            "sizes": sizes,
            "get_in": get_in,
            "setups": setups,
            "trend": trend,
            "structure": self._structure_for(symbol, features, spec),
            "history": self._history(symbol, features),
        }

    # --- every setup at once -------------------------------------------------------------------

    def _setup_config(self, name: str) -> BotConfig:
        """This config with another setup named, for every symbol."""
        return self.config.with_setup(name)

    def _setups(
        self, symbol: str, features: pd.DataFrame, advice: Advice, tick: Any, spec: SymbolSpec, now: datetime
    ) -> list[dict[str, Any]]:
        """What each setup says about the last closed bar: the one on screen and the other three.

        The others are computed from the same bars once per closed bar, so watching all four costs
        no extra requests to the terminal. Their signals show on their tiles; they are announced
        like the main one only with ``alerts.other_setups`` on.
        """
        active = self.config.for_symbol(symbol).strategy.name
        key = features["time"].iloc[-1]
        cached = self._others.get(symbol)
        if cached is None or cached[0] != key:
            bars = features[[c for c in (*BAR_COLUMNS, "tick_volume") if c in features.columns]]
            frames: dict[str, Any] = {}
            for name in STRATEGIES:
                if name == active:
                    continue
                params = self._setup_config(name).for_symbol(symbol)
                try:
                    frames[name] = (params, compute_signals(
                        bars, params.strategy, self.config.timeframe, self._htf.get(symbol) or {}
                    ))  # fmt: skip
                except Exception:
                    log.debug("%s: %s setup not computed", symbol, name, exc_info=True)
            cached = (key, frames)
            self._others[symbol] = cached
        out = []
        for name in STRATEGIES:
            if name == active:
                out.append({"name": name, "label": SETUP_LABELS[name], "active": True, "action": advice.action,
                            "side": advice.side, "text": advice.headline})  # fmt: skip
                continue
            if name not in cached[1]:
                continue
            params, frame = cached[1][name]
            item = self._setup_summary(frame, params.strategy, advice, tick, spec)
            out.append({"name": name, "label": SETUP_LABELS[name], "active": False, **item})
            if item["action"] in ("BUY", "SELL", "SKIP") and self.config.alerts.other_setups:
                self._note_setup(symbol, name, params, frame, tick, spec, now, item["action"])
        return out

    def _setup_summary(
        self, frame: pd.DataFrame, p: Any, advice: Advice, tick: Any, spec: SymbolSpec
    ) -> dict[str, Any]:
        row = frame.iloc[-1]
        if advice.action == "CLOSED":
            return {"action": "CLOSED", "side": 0, "text": advice.headline}
        if pd.isna(row["atr"]):
            return {"action": "WAIT", "side": 0, "text": "Indicators are still warming up."}
        signal, raw = int(row["signal"]), int(row["raw_signal"])
        if signal:
            if advice.blocker:  # session, news, loss limits, positions, spread: the same for every setup
                return {
                    "action": "SKIP",
                    "side": signal,
                    "text": f"{WORD[signal]} signal, skipped: {advice.blocker}",
                }
            return {
                "action": WORD[signal],
                "side": signal,
                "text": f"{WORD[signal].title()} signal: {explain(row, p)}",
            }
        if raw:
            label = BLOCK_LABELS.get(row["block"], row["block"])
            return {"action": "BLOCKED", "side": raw, "text": f"{WORD[raw].title()} trigger blocked: {label}"}
        side = watch_side(row, p)
        return {"action": "WAIT", "side": side,
                "text": next_trigger(frame, p, side, tick, spec, self.config.timeframe)[0]}  # fmt: skip

    def _note_setup(
        self, symbol: str, name: str, params: Any, frame: pd.DataFrame, tick: Any, spec: SymbolSpec,
        now: datetime, action: str,
    ) -> None:  # fmt: skip
        """Announce, once, a signal from a setup that is not the one on screen, with its priced plan."""
        mark = (symbol, name, self.config.timeframe, frame["time"].iloc[-1], action)
        if mark in self._signal_seen:
            return
        if len(self._signal_seen) > 2000:
            self._signal_seen.clear()
        self._signal_seen.add(mark)
        engine = self._engines.get(name)
        if engine is None:
            engine = self._engines[name] = Engine(
                self._setup_config(name), self.broker, execute=False, live=True
            )
        advice = advise(engine, symbol, spec, params, frame, tick, now)
        label = SETUP_LABELS[name]
        self.activity.append({"time": now, "symbol": symbol, "action": advice.action,
                              "text": f"{label} setup: {advice.headline}. {advice.detail}"})  # fmt: skip
        lines = self._alert_text(symbol, advice, spec).split("\n")
        lines[0] += f" · {label} setup"
        self._send("\n".join(lines), "signal" if advice.actionable else "blocked")

    # --- trend ------------------------------------------------------------------------------------

    def _trend_for(self, symbol: str, features: pd.DataFrame, spec: SymbolSpec, now: datetime) -> dict | None:
        """The trend on the trading timeframe as of the last closed bar (recomputed once per bar)."""
        key = features["time"].iloc[-1]
        cached = self._trends.get(symbol)
        if cached is None or cached[0] != key:
            try:
                strategy = self.config.for_symbol(symbol).strategy
                found = describe(features, strategy, self.config.timeframe, spec, self.config.bar_minutes)
            except Exception:
                log.debug("%s: trend not computed", symbol, exc_info=True)
                return None
            cached = (key, found)
            self._trends[symbol] = cached
            self._note_trend(symbol, found, now)
        return cached[1]

    def _structure_for(self, symbol: str, features: pd.DataFrame, spec: SymbolSpec) -> dict | None:
        """Swings, zones, round numbers and patterns as of the last closed bar (once per bar)."""
        key = features["time"].iloc[-1]
        cached = self._structures.get(symbol)
        if cached is None or cached[0] != key:
            try:
                found = structure.describe(features, spec.digits)
            except Exception:
                log.debug("%s: structure not computed", symbol, exc_info=True)
                return None
            cached = (key, found)
            self._structures[symbol] = cached
        return cached[1]

    def _note_trend(self, symbol: str, trend: dict[str, Any], now: datetime) -> None:
        """Announce a new trend, and each step towards its end, once."""
        kind = self._trend_alerts.new(symbol, trend)
        if not kind:
            return
        word = "uptrend" if trend["direction"] > 0 else "downtrend"
        leading = " ".join(s["text"] for s in trend["signs"] if s["leading"])
        if kind == "start":
            was = trend["previous"]
            before = ""
            if was:
                bars = f"{was['age']} bar{'' if was['age'] == 1 else 's'}"
                before = f" The {'uptrend' if was['direction'] > 0 else 'downtrend'} before it lasted {bars}."
            title, action, text = "NEW TREND", "TREND", f"{trend['headline']}.{before}"
        elif kind == "weakening":
            title, action = "TREND WEAKENING", "TREND"
            text = (
                f"{trend['headline']}. {leading} In the stored history about three trends in ten in this "
                f"state ended within 10 bars, against one in seven when nothing shows."
            )
        else:
            title, action = "TREND ENDING", "ENDING"
            text = (
                f"{trend['headline']}. {leading} In the stored history about three {word}s in four in this "
                f"state ended within 10 bars. It says the trend is close to over, not which way price goes next."
            )
        self.activity.append({"time": now, "symbol": symbol, "action": action, "text": text})
        self._send(f"{title} · {symbol}\n{text}", "trend")

    def _history(self, symbol: str, features: pd.DataFrame) -> dict[str, Any]:
        """Recent signals and their simple outcomes, recomputed when a new bar arrives."""
        key = features["time"].iloc[-1]
        cached = self._recent.get(symbol)
        if cached is None or cached[0] != key:
            params = self.config.for_symbol(symbol)
            cached = (key, recent_signals(features, params, self.config.schedule, self.config.bar_minutes))
            self._recent[symbol] = cached
        return cached[1]

    def _spec(self, symbol: str, now: datetime) -> SymbolSpec:
        cached = self._specs.get(symbol)
        if cached is None or now - cached[1] > SPEC_REFRESH:
            cached = (self.broker.symbol(symbol), now)
            self._specs[symbol] = cached
        return cached[0]

    def _features_for(self, symbol: str, now: datetime) -> pd.DataFrame:
        """Recompute signals when a new bar should have closed (rate-limited while the market is shut)."""
        features = self._features.get(symbol)
        fetched = self._fetched.get(symbol, 0.0)
        if features is not None:
            bar = pd.Timedelta(minutes=self.config.bar_minutes)
            next_close = features["time"].iloc[-1] + 2 * bar
            due = pd.Timestamp(now) >= next_close and time.monotonic() - fetched >= 5
            if not due and time.monotonic() - fetched < 300:
                return features
        bars, htf = fetch_frames(self.broker, self.config, symbol)
        features = compute_signals(bars, self.config.for_symbol(symbol).strategy, self.config.timeframe, htf)
        self._htf[symbol] = htf
        self._fetched[symbol] = time.monotonic()
        with self._lock:
            self._features[symbol] = features
        return features

    def _preview_for(self, symbol: str, features: pd.DataFrame) -> pd.DataFrame | None:
        """The forming bar's would-be signals, refreshed every few seconds (early warning only)."""
        cached = self._previews.get(symbol)
        if cached and time.monotonic() - cached[0] < PREVIEW_SECONDS:
            return cached[1]
        try:
            preview = preview_features(self.broker, self.config, symbol, features, self._htf.get(symbol))
        except Exception:
            log.debug("%s: preview failed", symbol, exc_info=True)
            preview = None
        self._previews[symbol] = (time.monotonic(), preview)
        return preview

    def _note_forming(self, symbol: str, advice: Advice, now: datetime, spec: SymbolSpec) -> None:
        """Announce a forming trigger once per bar and side. It flickers; that is its nature."""
        early = advice.early
        if not early or not early["firing"] or advice.actionable:
            return
        key = (symbol, *self._setup_key(symbol), early["closes_at"], early["side"])
        if key in self._forming_seen:
            return
        if len(self._forming_seen) > 500:
            self._forming_seen.clear()
        self._forming_seen.add(key)
        word = WORD[early["side"]]
        where = ""
        if early["level"] is not None:
            where = f" if the {self.config.timeframe} bar closes {'above' if early['side'] > 0 else 'below'} {early['level']:.{spec.digits}f}"
        verdict = (
            "the filters would pass it"
            if early["passes"]
            else f"the filters would block it ({early['blocked_by']})"
        )
        text = (
            f"{word} trigger forming{where}. Decided at {early['closes_at']:%H:%M} UTC; "
            f"price now {early['price']:.{spec.digits}f}; {verdict}. Not confirmed, it can still disappear."
        )
        self.activity.append({"time": now, "symbol": symbol, "action": "FORMING", "text": text})
        self._send(f"FORMING (unconfirmed) · {symbol}\n{text}", "forming")

    def _note_failed(self, symbol: str, advice: Advice, now: datetime, spec: SymbolSpec) -> None:
        """Announce, once, a break that has just closed back through its level."""
        failed = advice.failed_break
        if not failed or failed["bars_ago"] != 0:
            return
        key = (symbol, *self._setup_key(symbol), failed["time"], f"failed{failed['side']}")
        if key in self._forming_seen:
            return
        if len(self._forming_seen) > 500:
            self._forming_seen.clear()
        self._forming_seen.add(key)
        up = failed["side"] > 0
        text = (
            f"The {WORD[failed['side']].lower()} break {'above' if up else 'below'} "
            f"{failed['level']:.{spec.digits}f} has failed: the {self.config.timeframe} bar that closed at "
            f"{failed['time']:%H:%M} UTC finished back {'below' if up else 'above'} it at "
            f"{failed['price']:.{spec.digits}f}. The breakout is not holding."
        )
        self.activity.append({"time": now, "symbol": symbol, "action": "FAILED", "text": text})
        self._send(f"BREAK FAILED · {symbol}\n{text}", "failed")

    def _reversal(self, p: Any, price: float | None, now: datetime) -> Reversal | None:
        """Has the market turned against this open position? None for a symbol that is not watched."""
        features = self._features.get(p.symbol)
        if features is None or p.symbol not in self._specs:
            return None
        try:
            key = (features["time"].iloc[-1], p.side, p.time_open)
            cached = self._watches.get(p.ticket)
            if cached is None or cached[0] != key:  # rebuilt once per closed bar, not every second
                cached = (key, watch_position(features, p.side, p.time_open, self.config.bar_minutes))
                self._watches[p.ticket] = cached
            spec = self._specs[p.symbol][0]
            preview = self._previews.get(p.symbol, (0.0, None))[1]
            strategy = self.config.for_symbol(p.symbol).strategy
            found = assess(
                features, p.side, p.time_open, strategy, self.config.timeframe, spec, preview, cached[1]
            )
        except Exception:
            log.debug("%s: reversal check failed", p.symbol, exc_info=True)
            return None
        self._note_reversal(p, found, spec, price, now)
        return found

    def _note_reversal(
        self, p: Any, found: Reversal, spec: SymbolSpec, price: float | None, now: datetime
    ) -> None:
        """Announce each new sign once, and a forming one once per bar."""
        fresh, forming = self._reversal_alerts.new(p.ticket, found)
        holding = holding_text(p, spec, price)
        if fresh:
            signs = " ".join(s["text"] for s in found.signs)
            text = f"{found.headline}. {signs} You hold {holding}"
            action = "EXIT" if found.level == EXIT else "REVERSAL"
            self.activity.append({"time": now, "symbol": p.symbol, "action": action, "text": text})
            log.warning("%s %s", p.symbol, text)
            if self._warns():
                self._send(
                    f"{'GET OUT' if found.level == EXIT else 'REVERSAL WARNING'} · {p.symbol}\n{text}", "exit"
                )
        elif forming:
            signs = " ".join(s["text"] for s in forming)
            text = (
                f"Forming, not confirmed, against your {WORD[p.side].lower()}: if the {self.config.timeframe} "
                f"bar closes at the current price at {found.forming_closes_at:%H:%M} UTC, {signs} "
                f"It can still disappear. You hold {holding}"
            )
            self.activity.append({"time": now, "symbol": p.symbol, "action": "REVERSAL", "text": text})
            if self._warns():
                self._send(f"REVERSAL FORMING (unconfirmed) · {p.symbol}\n{text}", "forming")

    def _warns(self) -> bool:
        """Reversal warnings reach the phone from one process only: while the trading loop holds
        the duty (this dashboard has only just started), it sends them."""
        return self.duty is None or self.duty.held()

    def _heartbeat(self, now: datetime) -> None:
        """STARTED on the first refresh, then RUNNING once each trading day when the session
        opens. A trading day with no such message means alerts are not running: the PC was off or
        asleep, or the dashboard was closed without a STOPPED message."""
        if self._heartbeat_day is None:
            self._heartbeat_day = now.date()
            self._send(
                f"STARTED · tradebot\nThe dashboard is watching {', '.join(self.symbols)} on {self.config.timeframe}, "
                f"{setup_text(self.config)} setup. A RUNNING message follows every trading "
                "day at the session open; if one is missing, alerts are not running.",
                "status",
            )
            return
        start, end = self.config.schedule.hours(now)
        if end - start >= 24:  # trading all day: the session opens at midnight UTC, a phone at 3 a.m.
            start = session_hours("london", now)[0]  # so the day's message comes at the London open
        if now.weekday() >= 5 or now.hour < start or self._heartbeat_day == now.date():
            return
        self._heartbeat_day = now.date()
        self._send(
            f"RUNNING · tradebot\nWatching {', '.join(self.symbols)} on {self.config.timeframe}, "
            f"{setup_text(self.config)} setup. Alerts are on.",
            "status",
        )

    def _heads_up(self, held: list, rows: list[dict], now: datetime) -> None:
        """Once per trade placed by hand on an instrument the bot trades, opened without a BUY, SELL
        or JOIN on screen for it: say so, and that it takes the bot's place on that instrument.
        Trades already open when the dashboard starts are not news. Always the MT5 account's trades,
        where the bot trades: with one of your own accounts selected, ``held`` is that account's."""
        if self.desk and self.desk.manual:
            terminal = getattr(self.broker, "terminal_positions", None)
            held = terminal(None) if terminal else held
        manual = [p for p in held if p.magic != self.config.magic]
        if self._manual_seen is None:
            self._manual_seen = {p.ticket for p in manual}
            return
        for p in manual:
            if p.ticket in self._manual_seen:
                continue
            self._manual_seen.add(p.ticket)
            row = next((r for r in rows if r.get("symbol") == p.symbol), None)
            advice = row.get("advice") if row else None
            if advice is None:
                continue  # not an instrument the bot trades, or no advice to compare with
            action = advice.get("action") if isinstance(advice, dict) else getattr(advice, "action", "")
            side = advice.get("side") if isinstance(advice, dict) else getattr(advice, "side", 0)
            headline = advice.get("headline") if isinstance(advice, dict) else getattr(advice, "headline", "")
            if action in ("BUY", "SELL", "JOIN") and side == p.side:
                continue  # backed by the dashboard
            word = "BUY" if p.side > 0 else "SELL"
            risk = self.config.for_symbol(p.symbol).risk
            effects = []  # what the bot does about it, as configured
            if risk.account_trades:
                if risk.max_positions_per_symbol <= 1:
                    effects.append(f"While it is open the bot takes no trade on {p.symbol}")
                effects.append("it counts toward the bot's loss and risk limits")
            sentence = ", and ".join(effects)
            after = f" {sentence[0].upper()}{sentence[1:]}." if sentence else ""
            text = (
                f"Your {word} {p.volume:g} {p.symbol} was opened without a bot entry on screen (it showed: "
                f"{headline}). Entries without a signal or join lost money in the stored history.{after}"
            )
            log.info("%s: %s", p.symbol, text)
            self.activity.append({"time": now, "symbol": p.symbol, "action": "NOTE", "text": text})
            self._send(f"NO SIGNAL · {p.symbol}\n{text}", "position")

    def _weekly(self, now: datetime) -> None:
        """Saturday from 08:00 UTC, once a week: the bot's scorecard (its own trades against the 50 /
        100 trade milestones), and your own trades beside it."""
        if not self.desk or now.weekday() != 5 or now.hour < 8:
            return
        week = tuple(now.isocalendar())[:2]
        if self._weekly_sent == week:
            return
        review = (self.desk.state(now) or {}).get("review") or {}
        bot = review.get("bot")
        if not bot:
            return
        self._weekly_sent = week
        lines = ["WEEKLY · tradebot", f"Bot scorecard: {bot['text']}"]
        own = review.get("own") or {}
        if own.get("trades") and own.get("avg_r") is not None:
            lines.append(
                f"Your own trades, last {review.get('days')} days: {own['trades']}, {own['avg_r']:+.2f}R each."
            )
        lines.append("Judge by the R per trade, not the dollars: the dollars follow the size.")
        self._send("\n".join(lines), "status")

    def _setup_key(self, symbol: str) -> tuple[str, str]:
        """The setup and timeframe on screen for ``symbol``: part of every once-only alert key."""
        return self.config.for_symbol(symbol).strategy.name, self.config.timeframe

    def _send(self, text: str, kind: str = "info") -> None:
        """To the phone. ``kind`` lets a channel pick what it sends: see ``notify.IMPORTANT``.
        A second dashboard sends nothing: the first one already says it all."""
        if self.duty is not None and not self.duty.primary:
            log.debug("not sent (another dashboard is running): %s", text.split("\n", 1)[0])
            return
        if self.notifier:
            self.notifier.send(text, kind)

    def _note(self, symbol: str, advice: Advice, now: datetime, spec: SymbolSpec | None = None) -> None:
        """Log advice changes worth seeing: new signals, skips and the return to waiting."""
        key = f"{advice.action}:{advice.bar_time}"
        previous = self._last_action.get(symbol)
        self._last_action[symbol] = key
        quiet = ("WAIT", "CLOSED", "TREND", "JOIN")  # states, not events: logged when they change
        if previous == key or (previous is None and advice.action in quiet):
            return
        if previous is not None and previous.split(":")[0] == advice.action and advice.action in quiet:
            return
        if advice.action in ("BUY", "SELL", "SKIP", "BLOCKED"):  # said already, before a switch?
            mark = (symbol, *self._setup_key(symbol), pd.Timestamp(advice.bar_time), advice.action)
            if mark in self._signal_seen:
                return
            if len(self._signal_seen) > 2000:
                self._signal_seen.clear()
            self._signal_seen.add(mark)
        self.activity.append(
            {
                "time": now,
                "symbol": symbol,
                "action": advice.action,
                "text": f"{advice.headline}. {advice.detail}",
            }
        )
        if advice.action in ("BUY", "SELL", "BLOCKED", "SKIP"):
            kind = "signal" if advice.action in ("BUY", "SELL") else "blocked"
            text = self._alert_text(symbol, advice, spec)
            record = self.record_line(symbol) if kind == "signal" else ""
            self._send(f"{text}\n{record}" if record else text, kind)

    def _alert_text(self, symbol: str, advice: Advice, spec: SymbolSpec | None, plan: Any = None) -> str:
        digits = spec.digits if spec else 5
        lines = [f"{advice.action} · {symbol}", f"{advice.headline}. {advice.detail}"]
        plan = plan or advice.plan
        if plan and advice.action != "SKIP":
            others = self.desk.size_lines(symbol, plan.side) if self.desk else []
            levels = (
                f"{WORD[plan.side]} plan: entry ~{plan.entry:.{digits}f} · SL {plan.stop:.{digits}f} · "
                f"TP {plan.target:.{digits}f}"
            )
            own = self.desk.broker.account_name() if self.desk else None
            if plan.volume:
                lines.append(
                    f"{levels} · {plan.volume} lots · risk {plan.risk_amount:,.2f} · "
                    f"target {plan.reward_amount:,.2f} · {self._sized_for(own)}"
                )
            elif others:
                lines.append(f"{levels} · no size on {own or 'the MetaTrader 5 account'} ({plan.rejected})")
            lines += others
        return "\n".join(lines)

    def record_line(self, symbol: str) -> str:
        """How often this setup's signals on this symbol have lost in the stored history.

        That share is the honest answer to "is this a false signal?": nothing measured at the
        signal bar has told the losers from the winners (scripts/research_false_signals.py).
        """
        record = self.track_record or {}
        mine = (record.get("symbols") or {}).get(symbol) or {}
        trades = int(mine.get("trades") or 0)
        if trades < 20 or mine.get("win_rate") is None or not record.get("start"):
            return ""
        won, setup = float(mine["win_rate"]), SETUP_LABELS[self.config.for_symbol(symbol).strategy.name]
        lost = round(100 - won)
        return (
            f"Record: {setup} on {symbol} {self.config.timeframe} won {won:.0f}% of {trades} trades since "
            f"{pd.Timestamp(record['start']).year}, average {float(mine.get('avg_r') or 0):+.2f}R. About "
            f"{lost} in 100 signals like this one lose, and nothing measured on the signal bar picks them out."
        )

    def _sized_for(self, own: str | None) -> str:
        """Which account a size belongs to. Without it, a size worked out on the terminal's demo
        account (100,000 USD) reads like advice for a small live account."""
        try:
            account = self.broker.account()
        except Exception:
            return own or "the MetaTrader 5 account"
        name = own or f"MT5 {getattr(self.broker, 'server_name', lambda: '')() or 'account'}"
        return f"sized for {name}, equity {account.equity:,.0f} {account.currency}"

    # --- orders --------------------------------------------------------------------------------

    def _safe_execute(self, order: _Order) -> dict[str, Any]:
        try:
            if order.duplicate is not None:
                return self.duplicate(order.duplicate)
            return self.execute(
                order.symbol, order.side, order.manual, override=order.override, volume=order.volume
            )
        except Exception as exc:
            log.exception("order failed")
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    def _order_gate(self) -> str | None:
        """Why the dashboard may not send an order right now, or None. The STOP file pauses every
        new entry from here too: Get in and Duplicate set aside the bot's limits, never that."""
        if self.desk and self.desk.manual:
            return f"orders are disabled: {REFUSAL}"
        stop = self.config.kill_switch_file
        if stop and Path(stop).exists():
            return f"new entries are paused: {stop} exists (delete it to resume)"
        account = self.broker.account()
        allowed = getattr(self.broker, "terminal_trading_allowed", lambda: False)()
        gate = trading_gate(self.config, account, self.live_flag, allowed)
        return None if gate.execute else f"orders are disabled: {gate.reason}"

    def execute(
        self,
        symbol: str,
        side: int,
        manual: bool = False,
        override: bool = False,
        volume: float | None = None,
    ) -> dict[str, Any]:
        """Place a trade if, right now, the gate is open and the engine's risk rules allow it.

        A bot trade must match the current advice. A manual trade is the user's own call on
        direction: no strategy signal is required, but the trading gate, every entry guard
        (session, news, loss limits, positions, spread) and the engine's sizing still apply.

        ``override`` ("Get in", manual only): a bot signal or join the limits held back is taken
        anyway, as the user's own call. Only the limits are set aside (loss limits, positions, open
        risk, margin, hours, news, spread): never a stale quote or bar, and the size is still the
        configured risk per trade.

        ``volume`` ("Your size", manual only): the lots you chose instead of the bot's size; see
        ``_own_size``.
        """
        if symbol not in self.symbols or side not in WORD:
            return {"ok": False, "message": "unknown symbol or side"}
        if override and not manual:
            return {"ok": False, "message": "Get in is your own call: send it as a manual order"}
        if volume is not None and (not manual or override):
            return {
                "ok": False,
                "message": "your size is your own call: send it as a manual order, without Get in",
            }
        closed = self._order_gate()
        if closed:
            return {"ok": False, "message": closed}
        now = self.broker.now()
        spec = self.broker.symbol(symbol)
        tick = self.broker.tick(symbol)
        params = self.config.for_symbol(symbol)
        features = self._features_for(symbol, now)
        advice = advise(self.engine, symbol, spec, params, features, tick, now)
        if volume is not None:
            return self._own_size(symbol, side, volume, spec, tick, params, features, advice, now)
        over = ""
        if manual:
            plan = advice.plans.get("buy" if side > 0 else "sell")
            if plan is None:
                return {"ok": False, "message": f"no plan available: {advice.headline}"}
            held = advice.blocker or plan.rejected
            if held and override:
                rule = get_in_rule(advice)
                if rule is None or advice.side != side:
                    return {
                        "ok": False,
                        "message": f"nothing to get in on: {advice.headline}. {advice.detail}",
                    }
                plan = trade_plan(
                    self.engine, symbol, spec, params, features.iloc[-1], tick, side, caps=False
                )
                if plan.rejected:
                    return {"ok": False, "message": f"cannot size it: {plan.rejected}"}
                over = rule
            elif held:
                return {"ok": False, "message": f"blocked by a risk rule: {held}"}
        else:
            plan = advice.plan
            if not advice.actionable or advice.side != side or plan is None or plan.rejected:
                return {"ok": False, "message": f"not advised any more: {advice.headline}. {advice.detail}"}

        if over:
            reason = f"manual (Get in): the bot's {advice.action.lower()} taken over its limit: {over}"
        else:
            reason = "manual: the user's call from the dashboard" if manual else "advisor: " + advice.detail
        label = f"over the bot's limit ({over})" if over else ""
        return self._place(symbol, side, plan.volume, plan.stop_distance, plan.target_distance, plan.stop,
                           plan.target, manual, reason, label, now)  # fmt: skip

    def _own_size(
        self, symbol: str, side: int, volume: float, spec: SymbolSpec, tick: Tick, params: SymbolParams,
        features: pd.DataFrame, advice: Advice, now: datetime,
    ) -> dict[str, Any]:  # fmt: skip
        """ "Your size": the bot's stop and target for ``side`` at the live price, with the lots you
        typed. Set aside: the size per trade (including the smallest lot being over it, as on gold
        in a small account) and the open-risk and margin caps, as for Get in. Still held: the
        trading gate, a fresh quote and bar, every entry guard (hours, news, loss limits,
        positions, spread), the broker's lot limits, and ``OWN_SIZE_MAX_RISK``."""
        if advice.plans.get("buy" if side > 0 else "sell") is None:
            return {"ok": False, "message": f"no plan available: {advice.headline}"}
        if advice.blocker:
            return {"ok": False, "message": f"blocked by a risk rule: {advice.blocker}"}
        if not math.isfinite(volume) or not spec.volume_min - 1e-9 <= volume <= spec.volume_max + 1e-9:
            return {
                "ok": False,
                "message": f"{volume:g} lots is outside the broker's {spec.volume_min:g} to {spec.volume_max:g}",
            }
        if abs(spec.normalize_volume(volume) - volume) > 1e-9:
            return {
                "ok": False,
                "message": f"{volume:g} lots is not a multiple of the lot step {spec.volume_step:g}",
            }
        row = features.iloc[-1]
        sized = self.engine.plan_entry(symbol, spec, params, row, tick.ask - tick.bid, side, caps=False)
        if isinstance(sized, Rejection) and sized.key != "size":  # the size is yours; nothing else is
            return {"ok": False, "message": f"cannot plan it: {sized.message}"}
        plan = trade_plan(self.engine, symbol, spec, params, row, tick, side, caps=False)
        equity = self.broker.account().equity
        loss = -self._money_at(symbol, side, volume, plan.entry, plan.stop)
        percent = loss / equity * 100 if equity > 0 else math.inf
        if not math.isfinite(percent) or percent > OWN_SIZE_MAX_RISK + 1e-9:
            return {
                "ok": False,
                "message": f"{volume:g} lots would lose {loss:,.2f} at the stop ({percent:.1f}% of equity): "
                f"more than the {OWN_SIZE_MAX_RISK:g}% one dashboard order may risk",
            }
        limit = params.risk.risk_percent
        over = f", over the bot's {limit:g}%" if percent > limit + 1e-9 else ""
        label = f"your size: {percent:.1f}% risk{over}"
        reason = (
            f"manual (your size): {volume:g} lots, {loss:,.2f} at the stop ({percent:.2f}% of equity{over})"
        )
        return self._place(symbol, side, volume, plan.stop_distance, plan.target_distance, plan.stop,
                           plan.target, True, reason, label, now)  # fmt: skip

    def duplicate_plan(self, p: Any) -> dict[str, Any]:
        """What "Duplicate" sends for the bot's open trade ``p``: the same side, lots, stop and
        target, at the live price; or ``refused`` and why. It is your own call (logged as manual),
        so the bot's position limits do not stop it."""
        if p.magic != self.config.magic:
            return {"refused": "only the bot's trades are duplicated here"}
        spec, tick, now = self.broker.symbol(p.symbol), self.broker.tick(p.symbol), self.broker.now()
        problem = tick.problem(now)
        if problem:
            return {"refused": problem}
        side, gap = p.side, spec.stops_level * spec.point
        entry = tick.ask if side > 0 else tick.bid
        if not p.sl:
            return {"refused": "the trade has no stop to copy"}
        if (side > 0 and not p.sl < tick.bid - gap) or (side < 0 and not p.sl > tick.ask + gap):
            return {"refused": "price is too close to the trade's stop to copy it"}
        if p.tp and ((side > 0 and not p.tp > tick.ask + gap) or (side < 0 and not p.tp < tick.bid - gap)):
            return {"refused": "price is too close to the trade's target to copy it"}
        volume = spec.normalize_volume(p.volume)
        if volume < spec.volume_min:
            return {"refused": f"{p.volume:g} lots is below the broker's minimum"}
        risk = -self._money_at(p.symbol, side, volume, entry, p.sl)
        reward = self._money_at(p.symbol, side, volume, entry, p.tp) if p.tp else None
        equity = self.broker.account().equity
        return {
            "ticket": p.ticket, "symbol": p.symbol, "side": side, "volume": volume, "entry": entry,
            "stop": p.sl, "target": p.tp or None, "sl_distance": (entry - p.sl) * side,
            "tp_distance": (p.tp - entry) * side if p.tp else 0.0, "risk_amount": risk, "reward_amount": reward,
            "risk_percent": risk / equity * 100 if equity > 0 else None,
        }  # fmt: skip

    def duplicate(self, ticket: int) -> dict[str, Any]:
        """Open a copy of the bot's trade ``ticket`` (see ``duplicate_plan``), as your own call."""
        closed = self._order_gate()
        if closed:
            return {"ok": False, "message": closed}
        p = next((x for x in self.broker.positions(None) if x.ticket == ticket), None)
        if p is None:
            return {"ok": False, "message": f"trade #{ticket} is not open any more"}
        if p.symbol not in self.symbols:
            return {"ok": False, "message": f"{p.symbol} is not on the watchlist"}
        plan = self.duplicate_plan(p)
        if "refused" in plan:
            return {"ok": False, "message": f"not duplicated: {plan['refused']}"}
        return self._place(p.symbol, p.side, plan["volume"], plan["sl_distance"], plan["tp_distance"], plan["stop"],
                           plan["target"], True, f"manual: duplicate of the bot's trade #{ticket}",
                           f"duplicate of #{ticket}", self.broker.now())  # fmt: skip

    def _money_at(self, symbol: str, side: int, volume: float, entry: float, price: float) -> float:
        """Account-currency result of ``volume`` lots from ``entry`` to ``price``."""
        calc = getattr(self.broker, "calc_profit", None)
        value = calc(symbol, side, volume, entry, price) if calc else None
        if value is not None and math.isfinite(value):
            return float(value)
        spec = self.broker.symbol(symbol)
        return (price - entry) * side / spec.tick_size * spec.tick_value * volume

    def _place(
        self, symbol: str, side: int, volume: float, sl_distance: float, tp_distance: float, stop: float | None,
        target: float | None, manual: bool, reason: str, label: str, now: datetime,
    ) -> dict[str, Any]:  # fmt: skip
        """Send the order, then journal it, put it in the activity feed and on the phone. A manual
        order carries "manual" in its comment: the review counts it as your call, not the bot's."""
        word = WORD[side]
        comment = f"{self.config.comment} manual" if manual else self.config.comment
        result = self.broker.open(
            OrderRequest(symbol, side, volume, sl_distance, tp_distance, comment), self.config.magic
        )
        if result.partial and result.volume:
            volume = result.volume  # only part was filled; the rest was cancelled by the broker
            label = f"{label} · {result.message}" if label else result.message
        if self._journal:
            self._journal.record(JournalEvent(
                now, "OPEN" if result.ok else "REJECTED", symbol, word, volume, result.price,
                stop, target, result.ticket, None, reason if result.ok else result.message, False,
            ))  # fmt: skip
        about = f" · {label}" if label else ""
        text = (
            f"{'Manual ' if manual else ''}{word} {volume:g} @ {result.price} · SL {stop} · TP {target or 'none'}{about}"
            if result.ok
            else f"{word} rejected by the broker: {result.message}"
        )
        self.activity.append(
            {"time": now, "symbol": symbol, "action": "ORDER" if result.ok else "REJECTED", "text": text}
        )
        log.warning("%s %s", symbol, text)
        self._send(f"ORDER · {symbol}\n{text}", "order")
        return {"ok": result.ok, "message": text, "ticket": result.ticket, "price": result.price}

    # --- history -------------------------------------------------------------------------------

    def load_track_record(self, data_dir: str | Path, names: dict[str, str]) -> None:
        """Each symbol's record for the setup on screen, from stored history: measured in a
        separate low-priority process and kept per setup and timeframe (``ui.track``).

        ``names`` maps the broker's symbol names to the configured ones used for the CSV files.
        """
        self._track_args = (data_dir, names)
        config = self.config  # the setup or timeframe may be switched while this runs
        self._track_pending = True
        try:
            record = self.tracks.get(config, data_dir, names)
        except NoRecord as exc:
            if config is self.config:
                log.info("no track record: %s", exc)
                self._track_pending = False
            return
        except Exception:
            log.exception("track record failed")
            if config is self.config:
                self._track_pending = False
            return
        if config is not self.config:
            return  # switched while this ran: the newer run owns the screen
        self.track_record = record
        self._track_pending = False


def get_in_rule(advice: Advice) -> str | None:
    """The limit that holds back the bot's own entry on ``advice.side`` (a signal it skipped, or a
    join), which "Get in" may set aside as your own call; None when there is nothing to get in on
    or the hold-up is the data (a stale quote or bar), the STOP file (your own pause on every
    new entry) or a closed exchange (the broker would refuse the order), which nobody should
    trade through."""
    if advice.action not in ("SKIP", "JOIN") or advice.side not in WORD or advice.plan is None:
        return None
    held = advice.blocker or advice.plan.rejected
    if not held or held.startswith(("data:", "kill switch", "market:")):
        return None  # a stale quote or bar, the STOP file, a closed exchange: not limits
    return held


def _clip_line(line: list[float], offset: int) -> list[float]:
    """A line [i0, p0, i1, p1] in frame positions, moved to chart positions and cut at the left edge."""
    a, pa, b, pb = line
    if a < offset and b != a:
        pa, a = pa + (pb - pa) * (offset - a) / (b - a), offset
    return [a - offset, pa, b - offset, pb]


def clean(value: Any) -> Any:
    """JSON-safe copy: datetimes as UTC ISO strings, numpy scalars as Python, NaN/inf as None."""
    if is_dataclass(value) and not isinstance(value, type):
        return clean(asdict(value))
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
