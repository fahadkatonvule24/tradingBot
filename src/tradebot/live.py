"""Live / dry-run trading loop on top of the MT5 adapter."""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from tradebot.broker.base import Account, Broker
from tradebot.config import TIMEFRAME_MINUTES, BotConfig
from tradebot.engine import Engine
from tradebot.reversal import EXIT, Alerts, assess, holding_text
from tradebot.strategy import compute_signals

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gate:
    execute: bool
    reason: str


def trading_gate(config: BotConfig, account: Account, live_flag: bool, terminal_allows: bool) -> Gate:
    """Live orders need *all* of: dry_run=false, live_trading_confirm=true, --live on the command
    line, algo trading enabled in the terminal, and allow_real_account=true for a real-money account."""
    if config.dry_run:
        return Gate(False, "dry_run is true in the config")
    if not config.live_trading_confirm:
        return Gate(False, "live_trading_confirm is false in the config")
    if not live_flag:
        return Gate(False, "--live was not passed on the command line")
    if account.is_real and not config.allow_real_account:
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

    def ok(self) -> bool:
        """Call before using the broker: True if the link works (reconnecting first if it can)."""
        connected = getattr(self.broker, "connected", None)
        if connected is None or connected():
            self._recovered()
            return True
        now = self._clock()
        if self.down_since is None:
            self.down_since, self.attempts, self._next_try = now, 0, now
            detail = getattr(self.broker, "last_error_text", lambda: "no details")()
            log.error(
                "lost the connection to MetaTrader 5: %s. Is the terminal open? Retrying until it is back.",
                detail,
            )
        if now >= self._next_try:
            self._next_try = now + self.DELAYS[min(self.attempts, len(self.DELAYS) - 1)]
            self.attempts += 1
            if self.broker.reconnect():
                self._recovered()
                return True
        return False

    def _recovered(self) -> None:
        if self.down_since is not None:
            log.info("reconnected to MetaTrader 5 after %.0f s", self._clock() - self.down_since)
            self.down_since, self.attempts = None, 0


NOTIFY_KINDS = {"open", "dry_open", "close", "partial", "rejected"}


class LiveRunner:
    def __init__(
        self, config: BotConfig, broker: Broker, engine: Engine, symbols: list[str], notifier=None
    ) -> None:
        self.config, self.broker, self.engine, self.symbols = config, broker, engine, symbols
        self.notifier = notifier
        self._stop = threading.Event()
        self._link = Link(broker)
        self._features: dict[str, pd.DataFrame] = {}
        self._reversals = Alerts()

    def stop(self, *_: object) -> None:
        log.info("stop requested; finishing the current cycle")
        self._stop.set()

    def cycle(self) -> None:
        if not self._link.ok():
            return  # terminal unreachable: nothing can be read or managed until it is back
        for symbol in self.symbols:
            try:
                self._process(symbol)
            except Exception:  # one bad symbol must not stop the others
                if not self._link.ok():
                    return
                log.exception("%s: cycle failed", symbol)
        try:
            self._warn_reversals()
        except Exception:  # a warning must never stop the trading loop
            log.debug("reversal check failed", exc_info=True)
        if self.notifier:
            for event in self.engine.events:
                if event.kind in NOTIFY_KINDS:
                    self.notifier.send(f"{event.symbol}: {event.detail}")
        self.engine.events.clear()

    def _warn_reversals(self) -> None:
        """Log and send, once per sign, when the market turns against any open position on a
        watched symbol: the bot's own or one opened by hand."""
        held = [p for p in self.broker.positions(None) if p.symbol in self._features]
        self._reversals.forget({p.ticket for p in held})
        for p in held:
            spec = self.broker.symbol(p.symbol)
            strategy = self.config.for_symbol(p.symbol).strategy
            found = assess(
                self._features[p.symbol], p.side, p.time_open, strategy, self.config.timeframe, spec
            )
            fresh, _ = self._reversals.new(p.ticket, found)
            if not fresh:
                continue
            tick = self.broker.tick(p.symbol)
            text = (
                f"{found.headline}. {' '.join(s['text'] for s in found.signs)} "
                f"You hold {holding_text(p, spec, tick.bid if p.side > 0 else tick.ask)}"
            )
            log.warning("%s: %s", p.symbol, text)
            if self.notifier:
                title = "GET OUT" if found.level == EXIT else "REVERSAL WARNING"
                self.notifier.send(f"{title} · {p.symbol}\n{text}")

    def _process(self, symbol: str) -> None:
        params = self.config.for_symbol(symbol)
        spec = self.broker.symbol(symbol)
        features = latest_features(self.broker, self.config, symbol)
        self._features[symbol] = features
        row = features.iloc[-1].copy()
        now = self.broker.now()
        bar = timedelta(minutes=self.config.bar_minutes)
        age = now - (row["time"] + bar).to_pydatetime()
        if age > bar * self.config.schedule.max_bar_age_bars:
            log.info("%s: last closed bar is %s old (market closed?); managing positions only", symbol, age)
            row["signal"] = 0
        tick = self.broker.tick(symbol)
        self.engine.on_bar(symbol, spec, params, row, tick.ask - tick.bid, now)
        if row["signal"] == 0:
            log.debug("%s: no signal on bar %s (%s)", symbol, row["time"], row.get("block") or "no trigger")

    def run(self, once: bool = False) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
        while not self._stop.is_set():
            self.cycle()
            if once:
                return
            self._stop.wait(self.config.poll_seconds)


HTF_HISTORY_MULTIPLE = 6
"""Higher-timeframe bars fetched per trend-EMA period. An EMA seeded n bars back still carries
(1 - 2/(period+1))^n of its seed: 2% after 2 periods (what v3.0 fetched, enough to flip the trend
filter near a crossing), 0.0006% after 6. Live trading must see the same trend as the backtest."""


def latest_features(broker: Broker, config: BotConfig, symbol: str) -> pd.DataFrame:
    """Signals on the most recent closed bars, with the trend filter fed by real higher-timeframe bars."""
    bars, htf = fetch_frames(broker, config, symbol)
    return compute_signals(bars, config.for_symbol(symbol).strategy, config.timeframe, htf)


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
    bar = forming(symbol, config.timeframe) if forming else None
    if bar is None or pd.Timestamp(bar["time"]) <= closed["time"].iloc[-1]:
        return None
    strategy = config.for_symbol(symbol).strategy
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
    return compute_signals(base, strategy, config.timeframe, htf)


def describe_bar(row: pd.Series) -> str:
    return f"{row['time']:%Y-%m-%d %H:%M} close={row['close']} signal={int(row['signal'])}"
