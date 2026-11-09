"""Simulated broker for backtests.

Execution model (deliberately conservative):
- Bars are *bid* prices, as MetaTrader provides them. Buys fill at ask = bid + spread; sells
  close at ask. The spread is the bar's recorded spread or a configured floor, whichever is larger.
- Market orders decided at a bar's close fill at that close (plus spread and slippage).
- Stops and targets are checked against the next bars' high/low. A gap through a level fills at
  the bar's open, not the level. If one bar touches both the stop and the target, the stop is
  assumed to have been hit first.
- Commission is charged per lot actually traded (v23 charged it on the minimum lot only).
- Pip value is recomputed from the current price for USD-quoted and USD-based pairs instead of
  using one snapshot for the whole history.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from tradebot.broker.base import (
    BUY,
    Account,
    Deal,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)
from tradebot.config import BacktestParams


@dataclass
class ClosedTrade:
    ticket: int
    symbol: str
    side: int
    volume: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    pnl: float
    risk_amount: float
    exit_reason: str
    bars_held: int

    @property
    def r_multiple(self) -> float:
        return self.pnl / self.risk_amount if self.risk_amount else 0.0


@dataclass
class _Ledger:
    entry_index: int
    risk_amount: float
    realized: float
    closed_volume: float = 0.0
    exit_value: float = 0.0


class SimBroker:
    def __init__(
        self,
        specs: dict[str, SymbolSpec],
        params: dict[str, BacktestParams],
        initial_balance: float,
        account_currency: str = "USD",
        leverage: int = 100,
    ) -> None:
        self.specs = specs
        self.params = params
        self.currency = account_currency
        self.leverage = leverage
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self._now: datetime | None = None
        self._bars: dict[str, pd.Series] = {}
        self._bar_index: dict[str, int] = {}
        self._positions: dict[int, Position] = {}
        self._ledgers: dict[int, _Ledger] = {}
        self._deals: list[Deal] = []
        self._deal_times: list[datetime] = []
        self._ticket = 0
        self.trades: list[ClosedTrade] = []
        self.equity_curve: list[tuple[datetime, float]] = []

    # --- driving the simulation ---------------------------------------------------------------

    def start_bar(self, symbol: str, bar: pd.Series, close_time: datetime) -> None:
        """Advance ``symbol`` to a newly closed bar: trigger stops/targets inside it, then mark it."""
        self._bar_index[symbol] = self._bar_index.get(symbol, -1) + 1
        self._bars[symbol] = bar
        self._now = close_time
        for position in [p for p in self._positions.values() if p.symbol == symbol]:
            self._check_exits(position, bar)

    def record_equity(self, when: datetime) -> None:
        self.equity_curve.append((when, self.account().equity))

    def close_all(self, reason: str = "end of test") -> None:
        for position in list(self._positions.values()):
            self._close_volume(position, position.volume, self._exit_price(position), reason)

    # --- Broker interface ---------------------------------------------------------------------

    def now(self) -> datetime:
        assert self._now is not None, "simulation not started"
        return self._now

    def account(self) -> Account:
        floating = sum(self._mark(p) for p in self._positions.values())
        margin = sum(self.margin_required(p.symbol, p.side, p.volume) or 0 for p in self._positions.values())
        return Account(self.balance, self.balance + floating, margin, self.currency, False, self.leverage)

    def symbol(self, name: str) -> SymbolSpec:
        return self.specs[name]

    def tick(self, name: str) -> Tick:
        bar = self._bars[name]
        bid = float(bar["close"])
        return Tick(self.now(), bid, bid + self.spread(name, bar))

    def bars(self, name: str, timeframe: str, count: int) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError("the backtester feeds bars directly")

    def positions(self, magic: int) -> list[Position]:
        return list(self._positions.values())

    def deals(self, since: datetime, magic: int) -> list[Deal]:
        return self._deals[bisect_left(self._deal_times, since) :]

    def open(self, request: OrderRequest, magic: int) -> OrderResult:
        spec = self.specs[request.symbol]
        bar = self._bars[request.symbol]
        slip = self.params[request.symbol].slippage_points * spec.point
        bid = float(bar["close"])
        price = bid + self.spread(request.symbol, bar) + slip if request.side == BUY else bid - slip
        sl = spec.round_price(price - request.side * request.sl_distance)
        tp = spec.round_price(price + request.side * request.tp_distance) if request.tp_distance else 0.0
        self._ticket += 1
        position = Position(
            ticket=self._ticket,
            symbol=request.symbol,
            side=request.side,
            volume=request.volume,
            price_open=price,
            sl=sl,
            tp=tp,
            time_open=self.now(),
            initial_volume=request.volume,
            initial_sl=sl,
            comment=request.comment,
        )
        commission = self._commission(request.symbol, request.volume)
        self.balance -= commission
        risk_amount = abs(price - sl) / spec.tick_size * self._tick_value(spec, price) * request.volume
        self._positions[position.ticket] = position
        self._ledgers[position.ticket] = _Ledger(self._bar_index[request.symbol], risk_amount, -commission)
        self._add_deal(position, request.volume, price, -commission, "in", request.comment)
        return OrderResult(True, position.ticket, price, "filled")

    def close(self, position: Position, volume: float | None, magic: int, comment: str) -> OrderResult:
        live = self._positions.get(position.ticket)
        if live is None:
            return OrderResult(False, message="position not found")
        volume = live.volume if volume is None else min(volume, live.volume)
        price = self._exit_price(live)
        self._close_volume(live, volume, price, comment or "close")
        return OrderResult(True, live.ticket, price, "closed")

    def modify(self, position: Position, sl: float, tp: float, magic: int) -> OrderResult:
        live = self._positions.get(position.ticket)
        if live is None:
            return OrderResult(False, message="position not found")
        spec = self.specs[live.symbol]
        live.sl, live.tp = spec.round_price(sl), spec.round_price(tp) if tp else 0.0
        return OrderResult(True, live.ticket)

    def margin_required(self, symbol: str, side: int, volume: float) -> float | None:
        spec = self.specs[symbol]
        bar = self._bars.get(symbol)
        price = float(bar["close"]) if bar is not None else 1.0
        notional = spec.contract_size * volume
        if spec.currency_base == self.currency:
            return notional / self.leverage
        return notional * price * self._quote_to_account(spec, price) / self.leverage

    # --- internals ----------------------------------------------------------------------------

    def spread(self, symbol: str, bar: pd.Series) -> float:
        spec = self.specs[symbol]
        recorded = float(bar.get("spread", 0) or 0)
        return max(recorded, self.params[symbol].min_spread_points) * spec.point

    def _tick_value(self, spec: SymbolSpec, price: float) -> float:
        if spec.currency_profit == self.currency:
            return spec.tick_size * spec.contract_size
        if spec.currency_base == self.currency:
            return spec.tick_size * spec.contract_size / price
        return spec.tick_value

    def _quote_to_account(self, spec: SymbolSpec, price: float) -> float:
        if spec.currency_profit == self.currency:
            return 1.0
        return spec.tick_value / (spec.tick_size * spec.contract_size)

    def _commission(self, symbol: str, volume: float) -> float:
        return self.params[symbol].commission_per_lot / 2 * volume

    def _pnl(self, position: Position, exit_price: float, volume: float) -> float:
        spec = self.specs[position.symbol]
        move = (exit_price - position.price_open) * position.side
        return move / spec.tick_size * self._tick_value(spec, exit_price) * volume

    def _exit_price(self, position: Position) -> float:
        bar = self._bars[position.symbol]
        spec = self.specs[position.symbol]
        slip = self.params[position.symbol].slippage_points * spec.point
        bid = float(bar["close"])
        return bid - slip if position.side == BUY else bid + self.spread(position.symbol, bar) + slip

    def _mark(self, position: Position) -> float:
        bar = self._bars.get(position.symbol)
        if bar is None:
            return 0.0
        bid = float(bar["close"])
        price = bid if position.side == BUY else bid + self.spread(position.symbol, bar)
        return self._pnl(position, price, position.volume)

    def _check_exits(self, p: Position, bar: pd.Series) -> None:
        spec = self.specs[p.symbol]
        slip = self.params[p.symbol].slippage_points * spec.point
        offset = 0.0 if p.side == BUY else self.spread(p.symbol, bar)  # sells exit at the ask
        o, h, low = float(bar["open"]) + offset, float(bar["high"]) + offset, float(bar["low"]) + offset
        stop_kind = "stop" if abs(p.sl - (p.initial_sl or p.sl)) < spec.point / 2 else "trailing stop"
        if p.side == BUY:
            if p.sl and o <= p.sl:
                return self._close_volume(p, p.volume, o, f"{stop_kind} (gap)")
            if p.tp and o >= p.tp:
                return self._close_volume(p, p.volume, o, "target (gap)")
            if p.sl and low <= p.sl:
                return self._close_volume(p, p.volume, p.sl - slip, stop_kind)
            if p.tp and h >= p.tp:
                return self._close_volume(p, p.volume, p.tp, "target")
        else:
            if p.sl and o >= p.sl:
                return self._close_volume(p, p.volume, o, f"{stop_kind} (gap)")
            if p.tp and o <= p.tp:
                return self._close_volume(p, p.volume, o, "target (gap)")
            if p.sl and h >= p.sl:
                return self._close_volume(p, p.volume, p.sl + slip, stop_kind)
            if p.tp and low <= p.tp:
                return self._close_volume(p, p.volume, p.tp, "target")
        return None

    def _close_volume(self, p: Position, volume: float, price: float, reason: str) -> None:
        pnl = self._pnl(p, price, volume) - self._commission(p.symbol, volume)
        self.balance += pnl
        self._add_deal(p, volume, price, pnl, "out", reason)
        ledger = self._ledgers[p.ticket]
        ledger.realized += pnl
        ledger.closed_volume += volume
        ledger.exit_value += price * volume
        p.volume = round(p.volume - volume, 8)
        if p.volume <= 1e-9:
            del self._positions[p.ticket]
            del self._ledgers[p.ticket]
            self.trades.append(
                ClosedTrade(
                    ticket=p.ticket,
                    symbol=p.symbol,
                    side=p.side,
                    volume=p.initial_volume or ledger.closed_volume,
                    entry_time=p.time_open,
                    entry_price=p.price_open,
                    exit_time=self.now(),
                    exit_price=ledger.exit_value / ledger.closed_volume,
                    pnl=ledger.realized,
                    risk_amount=ledger.risk_amount,
                    exit_reason=reason,
                    bars_held=self._bar_index[p.symbol] - ledger.entry_index,
                )
            )

    def _add_deal(
        self, p: Position, volume: float, price: float, profit: float, entry: str, comment: str
    ) -> None:
        side = p.side if entry == "in" else -p.side
        deal = Deal(
            len(self._deals) + 1, p.ticket, p.symbol, self.now(), side, volume, price, profit, entry, comment
        )
        self._deals.append(deal)
        self._deal_times.append(deal.time)
