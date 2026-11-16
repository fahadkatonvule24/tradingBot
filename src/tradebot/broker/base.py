"""Broker-neutral types and the interface the trading engine talks to.

The engine never imports MetaTrader5. It talks to a ``Broker``: ``MT5Broker`` for live trading,
``SimBroker`` for backtests. That is what lets the backtest run the exact same decision code
as live trading, and lets the whole engine be tested without a terminal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Protocol

import pandas as pd

BUY, SELL = 1, -1


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    """Account-currency value of one tick move for 1.0 lot (as reported by the broker)."""
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0
    currency_base: str = ""
    currency_profit: str = ""

    def round_price(self, price: float) -> float:
        if not math.isfinite(price) or not math.isfinite(self.tick_size) or self.tick_size <= 0:
            raise ValueError("price and tick size must be finite, with a positive tick size")
        tick = Decimal(str(self.tick_size))
        rounded = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick
        return round(float(rounded), self.digits)

    def normalize_volume(self, volume: float) -> float:
        """Round *down* to the volume step. Never rounds up: that would exceed the risk budget."""
        if any(not math.isfinite(v) or v <= 0 for v in (volume, self.volume_step, self.volume_max)):
            return 0.0
        step = Decimal(str(self.volume_step))
        limit = Decimal(str(min(volume, self.volume_max)))
        # Absorb float division noise, but retain the exact step (including 0.25 or 0.05 lots).
        steps = (limit / step + Decimal("1e-9")).to_integral_value(rounding=ROUND_FLOOR)
        return float(steps * step)


@dataclass(frozen=True)
class Account:
    balance: float
    equity: float
    margin: float
    currency: str = "USD"
    is_real: bool = False
    leverage: int = 100


@dataclass(frozen=True)
class Tick:
    time: datetime
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def problem(self, now: datetime) -> str | None:
        """Reject invalid prices or a stale quote before it can drive an order."""
        if not all(math.isfinite(v) and v > 0 for v in (self.bid, self.ask)) or self.ask < self.bid:
            return "quote has invalid bid/ask prices"
        try:
            quoted, current = pd.Timestamp(self.time), pd.Timestamp(now)
            if pd.isna(quoted) or pd.isna(current):
                return "quote timestamp is unavailable"
            if quoted.tzinfo is not None:
                quoted = quoted.tz_convert("UTC").tz_localize(None)
            if current.tzinfo is not None:
                current = current.tz_convert("UTC").tz_localize(None)
            age = (current - quoted).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return "quote timestamp is invalid"
        if age < -60:
            return "quote timestamp is more than 60 seconds in the future"
        if age > 120:
            return "quote is stale (more than 120 seconds old)"
        return None


@dataclass
class Position:
    ticket: int
    symbol: str
    side: int
    volume: float
    price_open: float
    sl: float
    tp: float
    time_open: datetime
    initial_volume: float | None = None
    initial_sl: float | None = None
    profit: float = 0.0
    comment: str = ""
    magic: int = 0

    @property
    def initial_risk(self) -> float | None:
        """Price distance from entry to the *original* stop: the trade's 1R."""
        if not self.initial_sl:
            return None
        return abs(self.price_open - self.initial_sl) or None

    @property
    def partially_closed(self) -> bool:
        return self.initial_volume is not None and self.volume < self.initial_volume - 1e-9


@dataclass(frozen=True)
class Deal:
    ticket: int
    position: int
    symbol: str
    time: datetime
    side: int
    volume: float
    price: float
    profit: float
    """Net of commission, swap and fees."""
    entry: str  # "in" or "out"
    comment: str = ""
    magic: int = 0
    """The expert's id: the bot's own magic number, 0 for a trade placed by hand (phone or desktop)."""


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    ticket: int | None = None
    price: float | None = None
    message: str = ""
    retcode: int | None = None


@dataclass
class OrderRequest:
    """What the engine wants; the broker turns stop distances into prices at the fill price."""

    symbol: str
    side: int
    volume: float
    sl_distance: float
    tp_distance: float
    comment: str = ""
    meta: dict = field(default_factory=dict)


class Broker(Protocol):
    def now(self) -> datetime: ...
    def account(self) -> Account: ...
    def symbol(self, name: str) -> SymbolSpec: ...
    def tick(self, name: str) -> Tick: ...
    def bars(self, name: str, timeframe: str, count: int) -> pd.DataFrame: ...
    def positions(self, magic: int | None) -> list[Position]: ...
    def deals(self, since: datetime, magic: int | None) -> list[Deal]: ...
    def open(self, request: OrderRequest, magic: int) -> OrderResult: ...
    def close(self, position: Position, volume: float | None, magic: int, comment: str) -> OrderResult: ...
    def modify(self, position: Position, sl: float, tp: float, magic: int) -> OrderResult: ...
    def margin_required(self, symbol: str, side: int, volume: float) -> float | None: ...
