"""MetaTrader 5 adapter.

Differences from v23 worth knowing:
- **Closed bars only.** ``copy_rates_from_pos(..., 0, n)`` includes the still-forming bar; a
  signal computed on it can appear and vanish within the bar. It is dropped here.
- **UTC everywhere, daylight saving included.** MT5 stamps bars, ticks and deals in *server*
  time. The offset is measured from a live tick, and the broker's week-open pattern decides
  whether the server clock follows New York daylight saving (most FX brokers: UTC+2 in winter,
  UTC+3 in summer). Every timestamp, including a year of history, is converted with the offset
  that applied at that moment, so session hours, news blackouts and loss limits use one clock.
- **Restart-safe position state.** A position's original volume and stop (its 1R) are read back
  from the order history, so partial take-profit and R-based management survive restarts
  (v23 kept that in memory and could take partial profit twice).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from tradebot.broker.base import (
    BUY,
    SELL,
    Account,
    Deal,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)
from tradebot.broker.clock import ServerClock, detect_convention
from tradebot.config import TIMEFRAME_MINUTES, MT5Login

log = logging.getLogger(__name__)

RETCODE_INVALID_FILL = 10030


class MT5Error(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MT5Broker:
    def __init__(
        self, login: MT5Login, offset_hours: float | None = None, module: Any = None, clock=_utcnow
    ) -> None:
        if module is None:
            try:
                import importlib

                module = importlib.import_module("MetaTrader5")
            except ImportError as exc:  # pragma: no cover - depends on the platform
                raise MT5Error(
                    "MetaTrader5 is not installed (Windows only): pip install MetaTrader5"
                ) from exc
        self.mt5 = module
        self.login = login
        self._clock_rule: ServerClock | None = ServerClock(offset_hours) if offset_hours is not None else None
        self._clock = clock
        self._origin: dict[int, tuple[float | None, float | None]] = {}
        self._warned: set[str] = set()
        self._sleep = time.sleep

    # --- connection ---------------------------------------------------------------------------

    def connect(self, attempts: int = 6, wait_seconds: float = 3.0) -> Account:
        """Open the link to the terminal (the MetaTrader5 package starts the terminal if needed).

        A terminal that has only just started refuses the first calls with "Authorization failed"
        until it has logged in to the trade server, so a failure is retried for a short while.
        """
        kwargs = {k: v for k, v in (("path", self.login.path), ("login", self.login.login),
                                    ("password", self.login.password), ("server", self.login.server)) if v}  # fmt: skip
        for attempt in range(1, attempts + 1):
            if self.mt5.initialize(**kwargs):
                return self.account()
            code, message = self.mt5.last_error()
            if attempt < attempts:
                log.info("MT5 is not ready (%s: %s); trying again in %.0f s", code, message, wait_seconds)
                self._sleep(wait_seconds)
        raise MT5Error(
            f"MT5 initialize failed ({code}): {message}. Open the terminal and log in, or set MT5_LOGIN/MT5_PASSWORD/MT5_SERVER."
        )

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def connected(self) -> bool:
        """True while the link to the terminal answers (it drops if the terminal closes or restarts)."""
        try:
            return self.mt5.terminal_info() is not None
        except Exception:
            return False

    def reconnect(self) -> bool:
        """Re-open the link to the terminal. Safe to call repeatedly; False if it is still down."""
        try:
            self.mt5.shutdown()
        except Exception:
            log.debug("shutdown before reconnect failed", exc_info=True)
        try:
            self.connect(attempts=1)  # the caller (Link) does the waiting between tries
        except MT5Error:
            return False
        return True

    def last_error_text(self) -> str:
        try:
            code, message = self.mt5.last_error()
        except Exception:
            return "unknown error"
        return f"{message} ({code})"

    def terminal_trading_allowed(self) -> bool:
        info = self.mt5.terminal_info()
        return bool(info and info.trade_allowed)

    def server_name(self) -> str:
        info = self.mt5.account_info()
        return info.server if info else "?"

    def resolve(self, symbol: str) -> str | None:
        """Map a configured name to the broker's (EURUSD -> EURUSDm). Exact match wins."""
        if self.mt5.symbol_info(symbol) is not None:
            name = symbol
        else:
            candidates = [
                s.name
                for s in (self.mt5.symbols_get(f"*{symbol}*") or [])
                if s.name.upper().startswith(symbol.upper())
            ]
            if not candidates:
                return None
            name = min(candidates, key=len)
        info = self.mt5.symbol_info(name)
        if info is not None and not info.visible:
            self.mt5.symbol_select(name, True)
        return name

    @property
    def server_clock(self) -> ServerClock:
        if self._clock_rule is None:
            self._clock_rule = self._detect_clock()
        return self._clock_rule

    @property
    def offset(self) -> timedelta:
        """Server time minus UTC right now."""
        return self.server_clock.offset_at(self.now())

    def _detect_clock(self) -> ServerClock:
        """Measure today's offset from the freshest tick, then check the week-open pattern in the
        broker's own H1 history to see whether the server follows New York daylight saving."""
        now = self._clock()
        now_epoch = now.replace(tzinfo=timezone.utc).timestamp()
        for symbol in self.mt5.symbols_get() or []:
            if not getattr(symbol, "visible", True):
                continue
            tick = self.mt5.symbol_info_tick(symbol.name)
            if tick is None:
                continue
            diff_hours = (tick.time - now_epoch) / 3600
            if abs(diff_hours) > 14:
                continue
            offset = timedelta(hours=round(diff_hours * 4) / 4)
            rates = self.mt5.copy_rates_from_pos(symbol.name, self.mt5.TIMEFRAME_H1, 0, 12_000)
            times = [] if rates is None else rates["time"]
            clock = detect_convention(times, offset, now)
            log.info("server time is %s (from %s)", clock.describe(now), symbol.name)
            return clock
        log.warning(
            "no fresh tick (market closed?); assuming server time = UTC. Set server_utc_offset_hours to override."
        )
        return ServerClock(0.0)

    def _to_utc(self, server_epoch: float) -> datetime:
        return self.server_clock.epoch_to_utc(server_epoch)

    def _to_server_epoch(self, utc: datetime) -> int:
        return self.server_clock.utc_to_server_epoch(utc)

    # --- Broker interface ---------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def account(self) -> Account:
        a = self.mt5.account_info()
        if a is None:
            raise MT5Error(f"account_info failed: {self.mt5.last_error()}")
        is_real = a.trade_mode == getattr(self.mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
        return Account(a.balance, a.equity, a.margin, a.currency, is_real, a.leverage)

    def symbol(self, name: str) -> SymbolSpec:
        i = self.mt5.symbol_info(name)
        if i is None:
            raise MT5Error(f"symbol {name} not available")
        return SymbolSpec(
            name=name,
            digits=i.digits,
            point=i.point,
            tick_size=i.trade_tick_size or i.point,
            tick_value=self._tick_value(name, i),
            contract_size=i.trade_contract_size,
            volume_min=i.volume_min,
            volume_max=i.volume_max,
            volume_step=i.volume_step,
            stops_level=i.trade_stops_level,
            currency_base=i.currency_base,
            currency_profit=i.currency_profit,
        )

    def _tick_value(self, name: str, info: Any) -> float:
        """Account-currency value of one tick on 1 lot, from the terminal's own profit calculator.

        ``symbol_info().trade_tick_value`` is not reliable for CFDs: on MetaQuotes-Demo it reports
        0.1 for XAUUSD while a one-tick move on one lot actually pays 1.0. v23 sized positions from
        that field, so gold trades risked ~10x the configured percentage.
        """
        size = info.trade_tick_size or info.point
        tick = self.mt5.symbol_info_tick(name)
        if tick is not None and tick.ask:
            ticks = 1000  # measure over many ticks: profits are rounded to cents
            profit = self.mt5.order_calc_profit(
                self.mt5.ORDER_TYPE_BUY, name, 1.0, tick.ask, tick.ask + ticks * size
            )
            if profit:
                mismatch = info.trade_tick_value and abs(profit / ticks / info.trade_tick_value - 1) > 0.05
                if mismatch and name not in self._warned:
                    self._warned.add(name)
                    log.warning("%s: broker tick_value %.5g disagrees with the profit calculator (%.5g); using the latter",
                                name, info.trade_tick_value, profit / ticks)  # fmt: skip
                return profit / ticks
        return info.trade_tick_value

    def tick(self, name: str) -> Tick:
        t = self.mt5.symbol_info_tick(name)
        if t is None:
            raise MT5Error(f"no tick for {name}")
        return Tick(self._to_utc(t.time), t.bid, t.ask)

    def bars(self, name: str, timeframe: str, count: int) -> pd.DataFrame:
        tf = getattr(self.mt5, f"TIMEFRAME_{timeframe}")
        rates = self.mt5.copy_rates_from_pos(name, tf, 0, count + 1)
        if rates is None or len(rates) == 0:
            raise MT5Error(f"no {timeframe} bars for {name}: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = self.server_clock.to_utc(pd.to_datetime(df["time"], unit="s"))
        bar = pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        df = df[df["time"] + bar <= pd.Timestamp(self.now())]  # drop the forming bar
        if "spread" not in df:
            df["spread"] = 0
        return df[["time", "open", "high", "low", "close", "spread"]].tail(count).reset_index(drop=True)

    def forming_bar(self, name: str, timeframe: str) -> dict[str, Any] | None:
        """The bar that is still forming (UTC open time), or None when the market is closed."""
        rates = self.mt5.copy_rates_from_pos(name, getattr(self.mt5, f"TIMEFRAME_{timeframe}"), 0, 1)
        if rates is None or len(rates) == 0:
            return None
        last = rates[-1]
        opened = self._to_utc(float(last["time"]))
        if opened + timedelta(minutes=TIMEFRAME_MINUTES[timeframe]) <= self.now():
            return None
        return {k: float(last[k]) for k in ("open", "high", "low", "close")} | {"time": opened}

    def positions(self, magic: int | None) -> list[Position]:
        """Open positions with this magic number (``None``: every position on the account)."""
        out = []
        for p in self.mt5.positions_get() or []:
            if magic is not None and p.magic != magic:
                continue
            initial_volume, initial_sl = self.opening(p.ticket)
            out.append(
                Position(
                    ticket=p.ticket,
                    symbol=p.symbol,
                    side=BUY if p.type == self.mt5.POSITION_TYPE_BUY else SELL,
                    volume=p.volume,
                    price_open=p.price_open,
                    sl=p.sl,
                    tp=p.tp,
                    time_open=self._to_utc(p.time),
                    initial_volume=initial_volume or p.volume,
                    initial_sl=initial_sl or p.sl or None,
                    profit=p.profit,
                    comment=p.comment,
                    magic=p.magic,
                )
            )
        return out

    def opening(self, ticket: int) -> tuple[float | None, float | None]:
        """(volume, stop) of the order that opened a position, open or closed: the trade's 1R."""
        if ticket not in self._origin:
            orders = sorted(self.mt5.history_orders_get(position=ticket) or [], key=lambda o: o.time_setup)
            first = orders[0] if orders else None
            self._origin[ticket] = (first.volume_initial, first.sl or None) if first else (None, None)
        return self._origin[ticket]

    def deals(self, since: datetime, magic: int) -> list[Deal]:
        # Query a padded server-time range and filter in UTC: avoids missing deals when the
        # server clock runs ahead of the local one (a v23 loss-limit blind spot).
        start = self._to_server_epoch(since) - 86_400
        end = self._to_server_epoch(self.now()) + 86_400
        out = []
        for d in self.mt5.history_deals_get(start, end) or []:
            if d.magic != magic or d.entry not in (self.mt5.DEAL_ENTRY_IN, self.mt5.DEAL_ENTRY_OUT):
                continue
            when = self._to_utc(d.time)
            if when < since:
                continue
            net = d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
            out.append(
                Deal(
                    ticket=d.ticket,
                    position=d.position_id,
                    symbol=d.symbol,
                    time=when,
                    side=BUY if d.type == self.mt5.DEAL_TYPE_BUY else SELL,
                    volume=d.volume,
                    price=d.price,
                    profit=net,
                    entry="in" if d.entry == self.mt5.DEAL_ENTRY_IN else "out",
                    comment=d.comment,
                )
            )
        return sorted(out, key=lambda d: d.time)

    def open(self, request: OrderRequest, magic: int) -> OrderResult:
        spec = self.symbol(request.symbol)
        tick = self.mt5.symbol_info_tick(request.symbol)
        price = tick.ask if request.side == BUY else tick.bid
        order = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": request.volume,
            "type": self.mt5.ORDER_TYPE_BUY if request.side == BUY else self.mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": spec.round_price(price - request.side * request.sl_distance),
            "tp": spec.round_price(price + request.side * request.tp_distance)
            if request.tp_distance
            else 0.0,
            "deviation": 20,
            "magic": magic,
            "comment": request.comment[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
        }
        return self._send(order, request.symbol)

    def close(self, position: Position, volume: float | None, magic: int, comment: str) -> OrderResult:
        tick = self.mt5.symbol_info_tick(position.symbol)
        order = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "position": position.ticket,
            "volume": volume or position.volume,
            "type": self.mt5.ORDER_TYPE_SELL if position.side == BUY else self.mt5.ORDER_TYPE_BUY,
            "price": tick.bid if position.side == BUY else tick.ask,
            "deviation": 20,
            "magic": magic,
            "comment": comment[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
        }
        return self._send(order, position.symbol)

    def modify(self, position: Position, sl: float, tp: float, magic: int) -> OrderResult:
        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": position.ticket,
            "sl": sl,
            "tp": tp,
            "magic": magic,
        }
        return self._result(self.mt5.order_send(request))

    def calc_profit(
        self, symbol: str, side: int, volume: float, price_open: float, price_close: float
    ) -> float | None:
        """Account-currency profit of a position from ``price_open`` to ``price_close``, by the terminal."""
        kind = self.mt5.ORDER_TYPE_BUY if side == BUY else self.mt5.ORDER_TYPE_SELL
        return self.mt5.order_calc_profit(kind, symbol, volume, price_open, price_close)

    def margin_required(self, symbol: str, side: int, volume: float) -> float | None:
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        kind = self.mt5.ORDER_TYPE_BUY if side == BUY else self.mt5.ORDER_TYPE_SELL
        return self.mt5.order_calc_margin(kind, symbol, volume, tick.ask if side == BUY else tick.bid)

    # --- helpers ------------------------------------------------------------------------------

    def _filling_modes(self, symbol: str) -> list[int]:
        info = self.mt5.symbol_info(symbol)
        flags = int(getattr(info, "filling_mode", 0) or 0)
        modes = []
        if flags & 1:
            modes.append(self.mt5.ORDER_FILLING_FOK)
        if flags & 2:
            modes.append(self.mt5.ORDER_FILLING_IOC)
        for mode in (self.mt5.ORDER_FILLING_IOC, self.mt5.ORDER_FILLING_FOK, self.mt5.ORDER_FILLING_RETURN):
            if mode not in modes:
                modes.append(mode)
        return modes

    def _send(self, order: dict[str, Any], symbol: str) -> OrderResult:
        result = None
        for mode in self._filling_modes(symbol):
            result = self.mt5.order_send({**order, "type_filling": mode})
            if result is None or result.retcode != RETCODE_INVALID_FILL:
                break
            log.info("%s: filling mode %s rejected, trying the next one", symbol, mode)
        return self._result(result)

    def _result(self, result: Any) -> OrderResult:
        if result is None:
            code, message = self.mt5.last_error()
            return OrderResult(False, message=f"order_send failed ({code}): {message}")
        ok = result.retcode == self.mt5.TRADE_RETCODE_DONE
        return OrderResult(ok, result.order or None, result.price or None, result.comment, result.retcode)


def fetch_history(broker: MT5Broker, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    """Up to ``count`` closed bars, newest last, in UTC (for backtests via CSV)."""
    for n in (count, count // 2, count // 4):  # the terminal caps requests at its "max bars" setting
        try:
            return broker.bars(symbol, timeframe, n)
        except MT5Error:
            continue
    raise MT5Error(f"could not download {timeframe} history for {symbol}")
