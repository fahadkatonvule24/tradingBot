"""Live / dry-run trading loop on top of the MT5 adapter."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from tradebot import protect
from tradebot.book import Book, BookBroker
from tradebot.broker.base import Account, Broker, Position
from tradebot.broker.mt5 import MT5Error
from tradebot.config import TIMEFRAME_MINUTES, BotConfig
from tradebot.duty import Duty
from tradebot.engine import SIGNALS_ONLY, Engine
from tradebot.reversal import EXIT, Alerts, assess, holding_text
from tradebot.strategy import compute_signals, regular_session

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gate:
    execute: bool
    reason: str


def trading_gate(config: BotConfig, account: Account, live_flag: bool, terminal_allows: bool) -> Gate:
    """Live orders need *all* of: dry_run=false, live_trading_confirm=true, --live on the command
    line, algo trading enabled in the terminal, and allow_real_account=true for a real-money account."""
    if config.dry_run is not False:
        return Gate(False, "dry_run is true in the config")
    if config.live_trading_confirm is not True:
        return Gate(False, "live_trading_confirm is false in the config")
    if not live_flag:
        return Gate(False, "--live was not passed on the command line")
    if account.is_real and config.allow_real_account is not True:
        return Gate(False, "this is a REAL-money account and allow_real_account is false")
    if not terminal_allows:
        return Gate(False, "algo trading is disabled in the MT5 terminal (Tools > Options > Expert Advisors)")
    return Gate(True, "REAL-MONEY account" if account.is_real else "demo account")


class Link:
    """Watches the connection to the terminal and reconnects with a growing delay.

    Found the hard way: when MetaTrader 5 closed or restarted, every call failed with
    "IPC send failed" and the dashboard logged the same traceback once a second, forever.
    An outage is now logged once, retried at 1, 2, 5, 10 and then every 30 seconds, and
    logged once more when the link is back.
    """

    DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __init__(self, broker: Broker, clock=time.monotonic) -> None:
        self.broker, self._clock = broker, clock
        self.down_since: float | None = None
        self.attempts = 0
        self._next_try = 0.0
        self.offline = False
        """While the link is down: the terminal answers, only its broker connection is lost."""

    def ok(self) -> bool:
        """Call before using the broker: True if the link works (reconnecting first if it can)."""
        connected = getattr(self.broker, "connected", None)
        if connected is None or connected():
            self._recovered()
            self.offline = False
            return True
        now = self._clock()
        # The terminal still answers and only its own link to the broker's server is down (the
        # internet dropped: MT5's journal on this PC showed 33 such drops in one night). MT5
        # reconnects to the server by itself, so the link to the terminal is left alone: no
        # shutdown and initialize while it does (the 23 Nov freeze came right after that churn).
        offline = self.offline = getattr(self.broker, "terminal_answers", lambda: False)()
        if self.down_since is None:
            self.down_since, self.attempts, self._next_try = now, 0, now
            if offline:
                log.warning(
                    "MetaTrader 5 lost its connection to the broker's server (the internet connection?); "
                    "waiting for it to reconnect"
                )
            else:
                detail = getattr(self.broker, "last_error_text", lambda: "no details")()
                log.error(
                    "lost the connection to MetaTrader 5: %s. Is the terminal open? Retrying until it is back.",
                    detail,
                )
        if offline:
            return False
        if now >= self._next_try:
            self._next_try = now + self.DELAYS[min(self.attempts, len(self.DELAYS) - 1)]
            self.attempts += 1
            reconnect = getattr(self.broker, "reconnect", None)  # MT5Broker has it; a broker without a
            if reconnect is not None and reconnect():  # link to lose has no `connected` either
                self._recovered()
                self.offline = False
                return True
        return False

    def _recovered(self) -> None:
        if self.down_since is not None:
            log.info("reconnected to MetaTrader 5 after %.0f s", self._clock() - self.down_since)
            self.down_since, self.attempts = None, 0


NOTIFY_KINDS = {"open", "dry_open", "close", "partial", "rejected"}


def held_back_signal(detail: str) -> bool:
    """A skip event that is a real signal of an auto-traded symbol held back by a limit (open
    risk, loss limits, news, session...), not a data hold-up, a signals-only symbol, or a signal
    the bot already traded on this bar (seen again after a restart)."""
    return (
        " signal skipped: " in detail
        and SIGNALS_ONLY not in detail
        and not any(f"skipped: {why}:" in detail for why in ("data", "duplicate"))
        and not ("skipped: positions:" in detail and " open on " in detail)  # already in that market
    )


HEARTBEAT_FILE = "tradebot.loop.heartbeat"
"""Rewritten by the trading loop when it starts and after every cycle, next to the journal.
``scripts/start_bot.ps1`` restarts a loop whose heartbeat has gone quiet. Found the hard way: a
call into the MetaTrader5 package has no timeout, and on 23 Nov 2026 one hung for 2 h 18 min
after the terminal reconnected. The process stayed alive, so it looked like a running loop,
while a signal was entered 39 minutes late and two others were missed."""

FROZEN_NOTE = "tradebot.loop.frozen"
"""Left next to the heartbeat by ``start_bot.ps1`` when it restarts a frozen loop: the new loop
logs it, sends it to the phone and deletes it."""


def resolve_symbols(broker: Broker, configured: Iterable[str]) -> list[str]:
    """Resolve the watchlist at startup and again when the terminal changes accounts."""
    resolve = getattr(broker, "resolve", lambda name: name)
    symbols = []
    for wanted in configured:
        name = resolve(wanted)
        if name is None:
            log.warning("symbol %s is not available for trading on this account; skipping it", wanted)
        elif name not in symbols:
            symbols.append(name)
    if not symbols:
        raise ValueError("none of the configured symbols are available on this account")
    return symbols


class LiveRunner:
    def __init__(
        self, config: BotConfig, broker: Broker, engine: Engine, symbols: list[str], notifier=None,
        *, duty: Duty | None = None, manage: bool = False, book: Book | None = None,
        gate: Callable[[], Gate] | None = None, heartbeat: Path | None = None,
    ) -> None:  # fmt: skip
        """``duty``: whether this loop or a running dashboard sends the reversal warnings and
        manages trades placed by hand (``tradebot.duty``); None = always this loop. ``manage``: do
        that management at all (``tradebot run --live`` only: a dry run sends nothing). ``book``:
        the first stop seen on each position, kept by the dashboard, for the 1R of a trade that
        got its stop after it opened. ``gate``: ``trading_gate`` on the account the terminal is
        logged in to now, asked again before every symbol while orders are allowed: the terminal
        can be switched to another account, a real-money one included, while this runs.
        ``heartbeat``: the file ``run`` rewrites after every cycle (``HEARTBEAT_FILE``); None
        writes none."""
        self.config, self.broker, self.engine, self.symbols = config, broker, engine, symbols
        self.notifier = notifier
        self.heartbeat = heartbeat
        self.duty, self.manage = duty, manage
        self._gate = gate
        self._allowed = engine.execute
        """What the start-up check allowed: a later check can stop orders, never allow more."""
        self._account: object = None
        self._stop = threading.Event()
        self._link = Link(broker)
        self._features: dict[str, pd.DataFrame] = {}
        self._reversals = Alerts()
        self._manager = protect.Manager()
        self._turned: dict[int, tuple[str, str]] = {}
        self._quiet: set[str] = set()
        """Symbols whose market-closed note is logged already (until a fresh bar arrives)."""
        self._book = BookBroker(broker, book, config.magic) if book is not None else None

    def stop(self, *_: object) -> None:
        log.info("stop requested; finishing the current cycle")
        self._stop.set()

    def cycle(self) -> None:
        if not self._link.ok():
            return  # terminal unreachable: nothing can be read or managed until it is back
        # Refresh the account/watchlist before iterating; an account change can replace suffixes.
        if not self._authorize():
            return
        for symbol in tuple(self.symbols):
            try:
                if not self._authorize():
                    return
                if symbol not in self.symbols:
                    continue  # the account changed again during this cycle
                self._process(symbol)
            except Exception as exc:  # one bad symbol must not stop the others
                if not self._link.ok():
                    return
                if isinstance(exc, MT5Error):  # a call refused while the terminal reconnects: no bug
                    log.warning("%s: skipped this cycle: %s", symbol, exc)
                else:
                    log.exception("%s: cycle failed", symbol)
        duty = self.duty is None or self.duty.held()
        try:
            self._warn_reversals(send=duty)
        except Exception:  # a warning must never stop the trading loop
            log.debug("reversal check failed", exc_info=True)
        if duty and self.manage:
            try:
                self._protect()
            except Exception:  # protecting stops must never stop the trading loop
                log.warning("automatic trade management failed", exc_info=True)
        if self.notifier:
            for event in self.engine.events:
                if event.kind in NOTIFY_KINDS:
                    self.notifier.send(f"{event.symbol}: {event.detail}", "order")
                elif event.kind == "signal" and duty:  # a signals-only symbol; a dashboard sends its own
                    self.notifier.send(f"{event.symbol}: {event.detail}", "signal")
                elif event.kind == "skip" and duty and self.engine.execute and held_back_signal(event.detail):
                    # the bot's own signal, held back by a limit: said once, like the dashboard does
                    self.notifier.send(
                        f"{event.symbol}: {event.detail}. The bot did not place it.", "skipped"
                    )
        self.engine.events.clear()

    def _authorize(self) -> bool:
        """Before each symbol: is the terminal still on the same account, and may orders still be
        sent on it? A switch to another account re-reads the loss limits from its history; a
        failed check stops orders (and this loop's trade management) until a check passes."""
        checked = True
        connected = getattr(self.broker, "connected", None)
        if connected is not None and not connected():
            # the link dropped during this cycle: end it here. Nothing can be sent anyway, and the
            # checks below would read "algo trading is disabled" and stop orders with a phone alert
            # (24 Nov 04:26: stopped and resumed 15 s apart). The Link reports it next cycle.
            return False
        try:
            identity = getattr(self.broker, "account_id", None)
            if identity is not None:
                now = identity()
                if self._account is not None and now != self._account:
                    self.engine.forget_account()
                    self._features.clear()
                    self._turned.clear()
                    self._reversals = Alerts()
                    self._manager = protect.Manager()
                    self.symbols = resolve_symbols(self.broker, self.config.symbols)
                self._account = now
            if not self._allowed:
                return True
            gate = self._gate() if self._gate is not None else Gate(True, "authorized account")
        except Exception as exc:  # the account cannot be read: no orders until it can
            checked = False
            gate = Gate(False, f"the account could not be checked ({type(exc).__name__}: {exc})")
        if self.duty is not None:
            if gate.execute and not self.duty.set_trading(True):
                gate = Gate(False, "another live trading loop is running in this folder")
            elif not gate.execute:
                self.duty.set_trading(False)
        if gate.execute == self.engine.execute:
            return checked
        self.engine.execute = self.manage = gate.execute
        if gate.execute:
            log.warning("LIVE TRADING resumed on %s: orders will be sent again", gate.reason)
            text = f"Orders resumed on the {gate.reason}."
        else:
            log.error("orders STOPPED: %s. Signals are only logged until that changes.", gate.reason)
            text = f"Orders stopped: {gate.reason}. Signals are only logged until that changes."
        if self.notifier:
            self.notifier.send(f"TRADING · {text}", "order")
        return checked

    def _positions(self) -> list[Position]:
        """Every open position on the account, each with its first stop as 1R."""
        if self._book is not None:
            return self._book.terminal_positions(None)
        return self.broker.positions(None)

    def _warn_reversals(self, send: bool = True) -> None:
        """Log and send, once per sign, when the market turns against any open position on a
        watched symbol: the bot's own or one opened by hand. With ``send`` False a running
        dashboard says it; the signs are still noted, so taking over later repeats none."""
        held = [p for p in self._positions() if p.symbol in self._features]
        open_tickets = {p.ticket for p in held}
        self._reversals.forget(open_tickets)
        for ticket in [t for t in self._turned if t not in open_tickets]:
            del self._turned[ticket]
        for p in held:
            spec = self.broker.symbol(p.symbol)
            strategy = self.config.for_symbol(p.symbol).strategy
            found = assess(
                self._features[p.symbol], p.side, p.time_open, strategy, self.config.timeframe, spec
            )
            self._turned[p.ticket] = (found.level, found.headline)
            fresh, _ = self._reversals.new(p.ticket, found)
            if not fresh or not send:
                continue
            tick = self.broker.tick(p.symbol)
            text = (
                f"{found.headline}. {' '.join(s['text'] for s in found.signs)} "
                f"You hold {holding_text(p, spec, tick.bid if p.side > 0 else tick.ask)}"
            )
            log.warning("%s: %s", p.symbol, text)
            if self.notifier:
                title = "GET OUT" if found.level == EXIT else "REVERSAL WARNING"
                self.notifier.send(f"{title} · {p.symbol}\n{text}", "exit")

    def _protect(self) -> None:
        """What the dashboard does for trades placed by hand while it runs (``tradebot.protect``):
        done here while no dashboard runs, so a dashboard restart leaves no trade unmanaged."""
        allowed = getattr(self.broker, "terminal_trading_allowed", lambda: False)()
        if not protect.breakeven_gate(self.config, self.broker.account(), allowed).on:
            return
        bars = {
            symbol: bar
            for symbol, frame in self._features.items()
            if (bar := protect.last_bar(frame, self.config.bar_minutes)) is not None
        }
        positions = self._positions()
        for done in self._manager.run(
            self.broker, positions, self.config, bars, self._turned, self.broker.now()
        ):
            if self.notifier:
                self.notifier.send(f"{done.title} · {done.symbol}\n{done.text}", "order")

    def _process(self, symbol: str) -> None:
        params = self.config.for_symbol(symbol)
        spec = self.broker.symbol(symbol)
        features = latest_features(self.broker, self.config, symbol)
        if features.empty:  # a stock with no regular-session bar in its history yet
            log.info("%s: no bar of the exchange's regular session yet; nothing to decide", symbol)
            return
        self._features[symbol] = features
        row = features.iloc[-1].copy()
        now = self.broker.now()
        bar = timedelta(minutes=self.config.bar_minutes)
        age = now - (row["time"] + bar).to_pydatetime()
        if age > bar * self.config.schedule.max_bar_age_bars:
            if symbol not in self._quiet:  # once per closed stretch, not on every 15 s poll
                self._quiet.add(symbol)
                log.info(
                    "%s: last closed bar is %s old (market closed?); managing positions only", symbol, age
                )
            row["signal"] = 0
            row["exit_signal"] = 0
            if "join_signal" in row:
                row["join_signal"] = 0
        else:
            self._quiet.discard(symbol)
        tick = self.broker.tick(symbol)
        self.engine.on_bar(symbol, spec, params, row, tick.ask - tick.bid, now)
        if row["signal"] == 0:
            log.debug("%s: no signal on bar %s (%s)", symbol, row["time"], row.get("block") or "no trigger")

    def run(self, once: bool = False) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
        self._report_frozen_restart()
        self._beat()
        while not self._stop.is_set():
            self.cycle()
            self._beat()
            if once:
                return
            self._stop.wait(self.config.poll_seconds)

    def _beat(self) -> None:
        """Tell ``start_bot.ps1`` this loop is still cycling (``HEARTBEAT_FILE``)."""
        if self.heartbeat is None:
            return
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.heartbeat.write_text(f"{stamp} pid {os.getpid()}\n", encoding="ascii")
        except OSError:  # a missed beat only risks a needless restart, never a trade
            log.debug("heartbeat not written to %s", self.heartbeat, exc_info=True)

    def _report_frozen_restart(self) -> None:
        """If ``start_bot.ps1`` restarted this loop because the last one froze, say so once."""
        if self.heartbeat is None:
            return
        note = self.heartbeat.with_name(FROZEN_NOTE)
        try:
            text = note.read_text(encoding="utf-8-sig").strip()  # PowerShell 5 writes a BOM
        except FileNotFoundError:
            return
        except OSError:
            log.debug("could not read %s", note, exc_info=True)
            return
        try:
            note.unlink()
        except OSError:
            log.debug("could not delete %s", note, exc_info=True)
        text = text or "The trading loop froze and was started again."
        log.warning("restarted after a freeze: %s", text)
        if self.notifier:
            self.notifier.send(f"TRADING · {text}", "failed")


HTF_HISTORY_MULTIPLE = 6
"""Higher-timeframe bars fetched per trend-EMA period. An EMA seeded n bars back still carries
(1 - 2/(period+1))^n of its seed: 2% after 2 periods (what v3.0 fetched, enough to flip the trend
filter near a crossing), 0.0006% after 6. Live trading must see the same trend as the backtest."""


def latest_features(broker: Broker, config: BotConfig, symbol: str) -> pd.DataFrame:
    """Signals on the most recent closed bars, with the trend filter fed by real higher-timeframe bars."""
    bars, htf = fetch_frames(broker, config, symbol)
    params = config.for_symbol(symbol)
    return compute_signals(bars, params.strategy, config.timeframe, htf, params.risk.market_hours)


def fetch_frames(
    broker: Broker, config: BotConfig, symbol: str
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Closed bars on the trading timeframe, and on each higher timeframe the trend filter reads."""
    strategy = config.for_symbol(symbol).strategy
    bars = broker.bars(symbol, config.timeframe, config.history_bars)
    htf = {
        tf: broker.bars(symbol, tf, strategy.trend_ema * HTF_HISTORY_MULTIPLE)
        for tf in higher_timeframes(config, strategy)
    }
    return bars, htf


def higher_timeframes(config: BotConfig, strategy) -> list[str]:
    """The trend filter's timeframes that are above the trading timeframe and so need their own bars."""
    if not strategy.use_trend_filter:
        return []
    return [
        tf for tf in strategy.trend_ladder(config.timeframe) if TIMEFRAME_MINUTES[tf] > config.bar_minutes
    ]


BAR_COLUMNS = ["time", "open", "high", "low", "close", "spread"]


def preview_features(
    broker: Broker,
    config: BotConfig,
    symbol: str,
    closed: pd.DataFrame,
    htf: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame | None:
    """Signals as they would be if the forming bar closed at the current price (None if there is none).

    Pass the higher-timeframe bars already fetched for ``closed`` as ``htf`` to avoid asking the
    terminal for them again: the dashboard calls this every few seconds for every symbol.

    For the early warning only: nothing here is ever traded. If a higher-timeframe bar closes at
    the same moment as the forming bar, it is included too, as it will be when the bar really closes.
    """
    forming = getattr(broker, "forming_bar", None)
    if forming is None:
        return None
    bar = forming(symbol, config.timeframe)
    if bar is None or pd.Timestamp(bar["time"]) <= closed["time"].iloc[-1]:
        return None
    market_hours = config.for_symbol(symbol).risk.market_hours
    if market_hours and regular_session(pd.DataFrame([bar]), market_hours, config.timeframe).empty:
        return None  # a pre-market bar: the regular session's signals do not read it
    # the forming bar is never joined, and the join columns cost a full liquidity scan each time
    strategy = replace(config.for_symbol(symbol).strategy, auto_join=False)
    columns = [*BAR_COLUMNS, *(c for c in ("tick_volume",) if c in closed.columns and c in bar)]
    forming_row = {**bar, "spread": closed["spread"].iloc[-1]}
    base = pd.concat([closed[columns], pd.DataFrame([forming_row])[columns]], ignore_index=True)
    known, htf = htf or {}, {}
    closes = pd.Timestamp(bar["time"]) + pd.Timedelta(minutes=config.bar_minutes)
    for tf in higher_timeframes(config, strategy):
        frame = known.get(tf)
        if frame is None:
            frame = broker.bars(symbol, tf, strategy.trend_ema * HTF_HISTORY_MULTIPLE)
        higher = forming(symbol, tf)
        if higher and pd.Timestamp(higher["time"]) + pd.Timedelta(minutes=TIMEFRAME_MINUTES[tf]) <= closes:
            frame = pd.concat([frame, pd.DataFrame([{**higher, "spread": 0}])], ignore_index=True)
        htf[tf] = frame
    return compute_signals(base, strategy, config.timeframe, htf, market_hours)


def describe_bar(row: pd.Series) -> str:
    return f"{row['time']:%Y-%m-%d %H:%M} close={row['close']} signal={int(row['signal'])}"
