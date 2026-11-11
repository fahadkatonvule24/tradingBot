"""The trading engine: one decision procedure for live trading and backtests.

Per symbol, once per *closed* bar:

1. Manage open positions: exit on an opposite signal or time stop, take partial profit,
   move the stop to breakeven, trail it. Stops only ever tighten.
2. If the bar produced a signal, run it through the guards (kill switch, session, news,
   loss limits, drawdown, streaks, trade count, position limits, spread, portfolio risk,
   margin) and, if everything passes, size the position from the stop distance and open it.

The engine only talks to the ``Broker`` interface, so the backtester drives this exact code
through ``SimBroker``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tradebot.broker.base import BUY, Account, Broker, Deal, OrderRequest, Position, SymbolSpec
from tradebot.config import BotConfig, RiskParams, SymbolParams
from tradebot.journal import Journal, JournalEvent
from tradebot.news import NewsCalendar
from tradebot.risk import drawdown_percent, loss_per_lot, open_risk, position_size, tighter_stop
from tradebot.strategy import explain

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    time: datetime
    kind: str  # open, close, partial, modify, skip, dry_open, ...
    symbol: str
    detail: str
    side: int = 0
    volume: float = 0.0
    price: float | None = None


@dataclass(frozen=True)
class EntryPlan:
    """A sized, risk-checked entry: what the engine would send, before it is sent."""

    symbol: str
    side: int
    volume: float
    sl_distance: float
    tp_distance: float
    risk_amount: float
    """Account-currency loss if the stop is hit."""
    reward_amount: float
    """Account-currency profit if the target is hit."""
    equity: float
    reason: str = ""

    @property
    def risk_percent(self) -> float:
        return self.risk_amount / self.equity * 100 if self.equity else 0.0


@dataclass(frozen=True)
class Rejection:
    key: str
    """Skip-counter bucket: size, portfolio risk, margin."""
    message: str


@dataclass(frozen=True)
class RiskStatus:
    """Where the account stands against each account-level limit. A limit of 0 is switched off."""

    day_pnl: float
    day_percent: float
    """Today's realised result (UTC day) as a percentage of the balance the day started with."""
    daily_limit: float
    week_pnl: float
    week_percent: float
    weekly_limit: float
    drawdown: float
    """Percent below the highest balance of the last ``drawdown_days`` days (live mode only)."""
    drawdown_limit: float
    drawdown_days: int
    losing_streak: int
    streak_limit: int
    entries_today: int
    entries_limit: int
    open_risk_percent: float = 0.0
    open_risk_limit: float = 0.0
    positions: int = 0
    positions_limit: int = 0
    blocked: str = ""
    """The first limit that forbids a new entry right now, as the engine words it."""


@dataclass
class Engine:
    config: BotConfig
    broker: Broker
    execute: bool = True
    """False = dry run: decisions are logged and journalled, nothing is sent."""
    journal: Journal | None = None
    live: bool = False
    """Live mode adds restart-safety checks against broker history and honours the kill switch."""
    events: list[Event] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _acted_bar: dict[str, pd.Timestamp] = field(default_factory=dict)
    _older_deals: tuple[datetime, int, list[Deal]] | None = None
    _news: NewsCalendar | None = None

    def __post_init__(self) -> None:
        self._news = NewsCalendar(self.config.schedule.news_file)

    # --- entry point per closed bar -----------------------------------------------------------

    def on_bar(
        self,
        symbol: str,
        spec: SymbolSpec,
        params: SymbolParams,
        row: pd.Series,
        spread: float,
        now: datetime,
    ) -> None:
        self._manage(symbol, spec, params, row, spread, now)
        bar_time = row["time"]
        if self._acted_bar.get(symbol) == bar_time:
            return  # each bar's signal is acted on once, however often we poll
        self._acted_bar[symbol] = bar_time
        if row["signal"] == 0:
            return
        reason = self.entry_blocker(symbol, spec, params, row, spread, now)
        if reason:
            self.skips[reason.split(":")[0]] += 1
            self._emit(
                now, "skip", symbol, f"{'buy' if row['signal'] > 0 else 'sell'} signal skipped: {reason}"
            )
            return
        self._enter(symbol, spec, params, row, spread, now)

    # --- position management ------------------------------------------------------------------

    def _manage(
        self,
        symbol: str,
        spec: SymbolSpec,
        params: SymbolParams,
        row: pd.Series,
        spread: float,
        now: datetime,
    ) -> None:
        m = params.management
        bid = float(row["close"])
        bar_minutes = self.config.bar_minutes
        for position in [p for p in self.broker.positions(self.config.magic) if p.symbol == symbol]:
            price = bid if position.side == BUY else bid + spread
            if m.close_on_reverse and row["signal"] == -position.side:
                self._close(position, None, now, "opposite signal")
                continue
            if m.max_hold_bars:
                held = (now - position.time_open) / timedelta(minutes=bar_minutes)
                if held >= m.max_hold_bars:
                    self._close(position, None, now, f"time stop ({m.max_hold_bars} bars)")
                    continue

            risk = position.initial_risk
            if not risk:
                continue  # not opened by this engine, or history unavailable: leave it alone
            profit_r = (price - position.price_open) * position.side / risk

            if m.partial_tp and not position.partially_closed and profit_r >= m.partial_tp_r:
                volume = spec.normalize_volume(position.volume * m.partial_tp_fraction)
                if volume >= spec.volume_min and position.volume - volume >= spec.volume_min - 1e-9:
                    self._close(position, volume, now, f"partial profit at {profit_r:.1f}R")

            new_sl = position.sl
            if profit_r >= m.breakeven_r:
                breakeven = position.price_open + position.side * m.breakeven_buffer_r * risk
                new_sl = tighter_stop(position.side, new_sl, breakeven)
            if profit_r >= m.trail_start_r and pd.notna(row["atr"]):
                trailed = price - position.side * m.trail_atr * float(row["atr"])
                new_sl = tighter_stop(position.side, new_sl, trailed)
            new_sl = spec.round_price(new_sl)
            if abs(new_sl - position.sl) >= spec.point and self._valid_stop(
                position, new_sl, bid, spread, spec
            ):
                self._modify(position, new_sl, now, profit_r)

    @staticmethod
    def _valid_stop(position: Position, sl: float, bid: float, spread: float, spec: SymbolSpec) -> bool:
        gap = spec.stops_level * spec.point
        if position.side == BUY:
            return sl < bid - gap
        return sl > bid + spread + gap

    # --- entry guards -------------------------------------------------------------------------

    def entry_blocker(
        self,
        symbol: str,
        spec: SymbolSpec,
        params: SymbolParams,
        row: pd.Series,
        spread: float,
        now: datetime,
    ) -> str | None:
        """The first account/schedule guard that forbids a new entry right now, or None."""
        r, sched = params.risk, self.config.schedule

        if self.live and Path(self.config.kill_switch_file).exists():
            return f"kill switch: {self.config.kill_switch_file} exists"
        closed = sched.closed(now)
        if closed:
            return f"session: {closed}"
        currencies = {spec.currency_base, spec.currency_profit} - {""}
        event = (
            self._news.blackout(now, currencies, sched.news_before_minutes, sched.news_after_minutes)
            if self._news
            else None
        )
        if event:
            return f"news: {event.currency} {event.title} at {event.time:%H:%M} UTC"

        status, today = self._limits(r, now, self.broker.account())
        if status.blocked:
            return status.blocked
        if self.live and any(
            d.entry == "in" and d.symbol == symbol for d in today if d.time >= _bar_close(row, self.config)
        ):
            return "duplicate: already entered on this bar (restart?)"

        positions = self.broker.positions(self.config.magic)
        same = [p for p in positions if p.symbol == symbol]
        if len(same) >= r.max_positions_per_symbol:
            return f"positions: {len(same)} open on {symbol}"
        if len(positions) >= r.max_total_positions:
            return f"positions: {len(positions)} open in total"

        atr_ = float(row["atr"])
        if spread > r.max_spread_atr * atr_:
            return f"spread: {spread / atr_:.2f} ATR > {r.max_spread_atr} ATR"
        return None

    def _limits(self, r: RiskParams, now: datetime, account: Account) -> tuple[RiskStatus, list[Deal]]:
        """The account-level limits, from the broker's own deal history (so they survive restarts).

        Returns the figures and today's deals. The drawdown limit is a live-only circuit breaker:
        applied to a research backtest it would only cut the test short.
        """
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        breaker = self.live and r.max_drawdown_percent > 0
        lookback = max(30, r.drawdown_days) if breaker else 30
        history = self._deal_history(day_start, lookback)
        week = [d for d in history if d.time >= week_start]
        today = [d for d in week if d.time >= day_start]
        blocked = ""
        figures = {}
        for label, deals, limit in (
            ("daily", today, r.daily_loss_limit_percent),
            ("weekly", week, r.weekly_loss_limit_percent),
        ):
            realized = sum(d.profit for d in deals)
            starting_balance = account.balance - realized
            figures[label] = (realized, realized / starting_balance * 100 if starting_balance > 0 else 0.0)
            hit = limit > 0 and starting_balance > 0 and realized <= -starting_balance * limit / 100
            if hit and not blocked:
                blocked = f"{label} loss limit: {realized:,.2f} ({realized / starting_balance:.1%})"
        drawdown = 0.0
        if breaker:
            since = day_start - timedelta(days=r.drawdown_days)
            drawdown = drawdown_percent([d for d in history if d.time >= since], account.balance)
            if drawdown >= r.max_drawdown_percent and not blocked:
                blocked = (
                    f"drawdown limit: {drawdown:.1f}% below the {r.drawdown_days}-day peak "
                    f"(limit {r.max_drawdown_percent:g}%), entries paused"
                )
        streak = self._losing_streak_today(history, day_start)
        if r.max_consecutive_losses and streak >= r.max_consecutive_losses and not blocked:
            blocked = f"losing streak: {r.max_consecutive_losses} losses in a row, paused until 00:00 UTC"
        entries_today = sum(1 for d in today if d.entry == "in")
        if r.max_trades_per_day and entries_today >= r.max_trades_per_day and not blocked:
            blocked = f"trade count: {entries_today} entries today"
        status = RiskStatus(
            day_pnl=figures["daily"][0],
            day_percent=figures["daily"][1],
            daily_limit=r.daily_loss_limit_percent,
            week_pnl=figures["weekly"][0],
            week_percent=figures["weekly"][1],
            weekly_limit=r.weekly_loss_limit_percent,
            drawdown=drawdown,
            drawdown_limit=r.max_drawdown_percent if breaker else 0.0,
            drawdown_days=r.drawdown_days,
            losing_streak=streak,
            streak_limit=r.max_consecutive_losses,
            entries_today=entries_today,
            entries_limit=r.max_trades_per_day,
            blocked=blocked,
        )
        return status, today

    def _deal_history(self, day_start: datetime, lookback: int) -> list[Deal]:
        """The bot's deals of the last ``lookback`` days, oldest first.

        Live, the limits are checked for every symbol every second. Deals from before today can no
        longer change, so they are read from the terminal once a day and only today's are re-read.
        """
        since = day_start - timedelta(days=lookback)
        if not self.live:
            return self.broker.deals(since, self.config.magic)
        if self._older_deals is None or self._older_deals[:2] != (day_start, lookback):
            older = [d for d in self.broker.deals(since, self.config.magic) if d.time < day_start]
            self._older_deals = (day_start, lookback, older)
        return self._older_deals[2] + self.broker.deals(day_start, self.config.magic)

    def risk_status(self, now: datetime) -> RiskStatus:
        """The limits plus what is open right now, for a person to read (the dashboard shows it)."""
        r = self.config.risk
        account = self.broker.account()
        status, _ = self._limits(r, now, account)
        positions = self.broker.positions(self.config.magic)
        committed = open_risk(positions, {p.symbol: self.broker.symbol(p.symbol) for p in positions})
        return replace(
            status,
            open_risk_percent=committed / account.equity * 100 if account.equity > 0 else 0.0,
            open_risk_limit=r.max_total_risk_percent,
            positions=len(positions),
            positions_limit=r.max_total_positions,
        )

    def _losing_streak_today(self, deals: list[Deal], day_start: datetime) -> int:
        """Losing positions in a row (by close time). Only a streak that ends today pauses trading."""
        open_tickets = {p.ticket for p in self.broker.positions(self.config.magic)}
        net: dict[int, float] = defaultdict(float)
        closed_at: dict[int, datetime] = {}
        for d in deals:
            net[d.position] += d.profit
            if d.entry == "out":
                closed_at[d.position] = d.time
        streak = 0
        for ticket in sorted(closed_at, key=closed_at.__getitem__, reverse=True):
            if ticket in open_tickets:
                continue
            if net[ticket] >= 0:
                break
            if streak == 0 and closed_at[ticket] < day_start:
                return 0
            streak += 1
        return streak

    # --- execution ----------------------------------------------------------------------------

    def plan_entry(
        self,
        symbol: str,
        spec: SymbolSpec,
        params: SymbolParams,
        row: pd.Series,
        spread: float,
        side: int,
    ) -> EntryPlan | Rejection:
        """Stop/target distances, volume and portfolio checks for a new position on ``side``.

        Shared by the engine and the advisor dashboard, so the advice is the trade the bot takes.
        """
        r = params.risk
        sl_distance, tp_distance = stop_distances(spec, r, float(row["atr"]), spread)
        account = self.broker.account()
        exact = self._exact_pnl(symbol, spec, side, sl_distance, tp_distance)
        tick_value = exact[0] / (sl_distance / spec.tick_size) if exact else None
        size = position_size(spec, account.equity, sl_distance, r, tick_value)
        if not size.ok:
            return Rejection("size", size.reason)
        positions = self.broker.positions(self.config.magic)
        committed = open_risk(positions, {p.symbol: self.broker.symbol(p.symbol) for p in positions})
        if (committed + size.risk_amount) / account.equity * 100 > r.max_total_risk_percent + 1e-9:
            return Rejection("portfolio risk", f"open risk would exceed {r.max_total_risk_percent}%")
        margin = self.broker.margin_required(symbol, side, size.volume)
        if r.max_margin_percent and margin is not None and account.equity > 0:
            projected = (account.margin + margin) / account.equity * 100
            if projected > r.max_margin_percent:
                return Rejection("margin", f"margin would reach {projected:.0f}%")
        return EntryPlan(
            symbol=symbol,
            side=side,
            volume=size.volume,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
            risk_amount=exact[0] * size.volume if exact else size.risk_amount,
            reward_amount=(exact[1] if exact else loss_per_lot(spec, tp_distance)) * size.volume,
            equity=account.equity,
            reason=explain(row, params.strategy) if row.get("raw_signal", 0) else "",
        )

    def _exact_pnl(
        self, symbol: str, spec: SymbolSpec, side: int, sl_distance: float, tp_distance: float
    ) -> tuple[float, float] | None:
        """(loss at the stop, profit at the target) per lot, by the broker's own profit calculator.

        Quote-currency conversion makes a pip worth slightly different amounts at the stop and at
        the target (USDJPY, USDCAD); a single tick value cannot capture that, the terminal can.
        """
        calc = getattr(self.broker, "calc_profit", None)
        if calc is None:
            return None
        tick = self.broker.tick(symbol)
        entry = tick.ask if side > 0 else tick.bid
        stop = spec.round_price(entry - side * sl_distance)
        target = spec.round_price(entry + side * tp_distance)
        loss, win = calc(symbol, side, 1.0, entry, stop), calc(symbol, side, 1.0, entry, target)
        if not loss or not win or loss >= 0 or win <= 0:
            return None
        return -loss, win

    def _enter(
        self,
        symbol: str,
        spec: SymbolSpec,
        params: SymbolParams,
        row: pd.Series,
        spread: float,
        now: datetime,
    ) -> None:
        side = int(row["signal"])
        plan = self.plan_entry(symbol, spec, params, row, spread, side)
        if isinstance(plan, Rejection):
            self.skips[plan.key] += 1
            self._emit(now, "skip", symbol, f"signal skipped: {plan.message}")
            return

        reason, volume = plan.reason, plan.volume
        word = "BUY" if side > 0 else "SELL"
        if not self.execute:
            self._emit(
                now,
                "dry_open",
                symbol,
                f"DRY RUN {word} {volume} · {reason}",
                side,
                volume,
                float(row["close"]),
            )
            entry = float(row["close"]) + (spread if side > 0 else 0.0)
            levels = self._levels(spec, plan, entry)
            self._journal(now, "DRY_OPEN", symbol, side, volume, entry, None, reason, *levels)
            return
        request = OrderRequest(symbol, side, volume, plan.sl_distance, plan.tp_distance, self.config.comment)
        result = self.broker.open(request, self.config.magic)
        if not result.ok:
            self._emit(now, "rejected", symbol, f"{word} rejected: {result.message}", side, volume)
            self._journal(now, "REJECTED", symbol, side, volume, None, None, result.message)
            return
        self._emit(
            now, "open", symbol, f"{word} {volume} @ {result.price} · {reason}", side, volume, result.price
        )
        levels = self._levels(spec, plan, result.price) if result.price else (None, None)
        self._journal(now, "OPEN", symbol, side, volume, result.price, result.ticket, reason, *levels)

    @staticmethod
    def _levels(spec: SymbolSpec, plan: EntryPlan, entry: float) -> tuple[float, float]:
        """Stop and target prices for an entry at ``entry``."""
        return (
            spec.round_price(entry - plan.side * plan.sl_distance),
            spec.round_price(entry + plan.side * plan.tp_distance),
        )

    def _close(self, position: Position, volume: float | None, now: datetime, why: str) -> None:
        kind = "partial" if volume is not None else "close"
        if not self.execute:
            self._emit(now, f"dry_{kind}", position.symbol, f"DRY RUN {kind} #{position.ticket}: {why}")
            return
        result = self.broker.close(position, volume, self.config.magic, why)
        detail = f"{kind} #{position.ticket} ({why})" + ("" if result.ok else f" failed: {result.message}")
        self._emit(
            now,
            kind if result.ok else "rejected",
            position.symbol,
            detail,
            -position.side,
            volume or position.volume,
            result.price,
        )
        if result.ok:
            self._journal(
                now,
                kind.upper(),
                position.symbol,
                -position.side,
                volume or position.volume,
                result.price,
                position.ticket,
                why,
            )

    def _modify(self, position: Position, sl: float, now: datetime, profit_r: float) -> None:
        if not self.execute:
            self._emit(now, "dry_modify", position.symbol, f"DRY RUN stop #{position.ticket} -> {sl}")
            return
        result = self.broker.modify(position, sl, position.tp, self.config.magic)
        if result.ok:
            self._emit(
                now,
                "modify",
                position.symbol,
                f"stop #{position.ticket} {position.sl} -> {sl} at {profit_r:.1f}R",
            )
        else:
            self._emit(
                now, "rejected", position.symbol, f"stop move #{position.ticket} rejected: {result.message}"
            )

    def _emit(
        self,
        now: datetime,
        kind: str,
        symbol: str,
        detail: str,
        side: int = 0,
        volume: float = 0.0,
        price: float | None = None,
    ) -> None:
        self.events.append(Event(now, kind, symbol, detail, side, volume, price))
        if self.live:
            log.info("%s %s", symbol, detail)

    def _journal(
        self,
        now: datetime,
        kind: str,
        symbol: str,
        side: int,
        volume: float,
        price: float | None,
        ticket: int | None,
        reason: str,
        sl: float | None = None,
        tp: float | None = None,
    ) -> None:
        if self.journal:
            self.journal.record(
                JournalEvent(
                    now,
                    kind,
                    symbol,
                    "BUY" if side > 0 else "SELL",
                    volume,
                    price,
                    sl,
                    tp,
                    ticket,
                    None,
                    reason,
                    not self.execute,
                )
            )


def stop_distances(spec: SymbolSpec, risk: RiskParams, atr_: float, spread: float) -> tuple[float, float]:
    """Stop and target distances from the fill price: ATR multiples, never inside the broker minimum."""
    min_distance = spec.stops_level * spec.point + spread
    return max(risk.sl_atr * atr_, min_distance), max(risk.tp_atr * atr_, min_distance)


def _bar_close(row: pd.Series, config: BotConfig) -> datetime:
    return (row["time"] + pd.Timedelta(minutes=config.bar_minutes)).to_pydatetime()
