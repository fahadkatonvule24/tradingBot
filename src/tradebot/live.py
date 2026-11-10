"""Live / dry-run trading loop on top of the MT5 adapter."""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from tradebot.broker.base import Account, Broker
from tradebot.config import TIMEFRAME_MINUTES, BotConfig
from tradebot.engine import Engine
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


class LiveRunner:
    def __init__(self, config: BotConfig, broker: Broker, engine: Engine, symbols: list[str]) -> None:
        self.config, self.broker, self.engine, self.symbols = config, broker, engine, symbols
        self._stop = threading.Event()

    def stop(self, *_: object) -> None:
        log.info("stop requested; finishing the current cycle")
        self._stop.set()

    def cycle(self) -> None:
        for symbol in self.symbols:
            try:
                self._process(symbol)
            except Exception:  # one bad symbol must not stop the others
                log.exception("%s: cycle failed", symbol)
        self.engine.events.clear()

    def _process(self, symbol: str) -> None:
        params = self.config.for_symbol(symbol)
        spec = self.broker.symbol(symbol)
        row = latest_features(self.broker, self.config, symbol).iloc[-1].copy()
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
    strategy = config.for_symbol(symbol).strategy
    bars = broker.bars(symbol, config.timeframe, config.history_bars)
    htf = None
    if strategy.use_trend_filter and TIMEFRAME_MINUTES[strategy.trend_timeframe] > config.bar_minutes:
        htf = broker.bars(symbol, strategy.trend_timeframe, strategy.trend_ema * HTF_HISTORY_MULTIPLE)
    return compute_signals(bars, strategy, config.timeframe, htf)


def describe_bar(row: pd.Series) -> str:
    return f"{row['time']:%Y-%m-%d %H:%M} close={row['close']} signal={int(row['signal'])}"
