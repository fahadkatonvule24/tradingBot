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
import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tradebot.broker.base import BUY, Account, Broker, Deal, OrderRequest, Position, SymbolSpec
from tradebot.config import BotConfig, RiskParams, SymbolParams, market_closed
from tradebot.earnings import EarningsCalendar
from tradebot.journal import Journal, JournalEvent
from tradebot.news import NewsCalendar
from tradebot.risk import drawdown_percent, loss_per_lot, open_risk, position_size, tighter_stop
from tradebot.strategy import explain

log = logging.getLogger(__name__)

SIGNALS_ONLY = "signals only (risk.auto_trade is off for this symbol): place it yourself if you want it"


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
    open_risk_percent: float | None = 0.0
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
    _data_logged: dict[str, str] = field(default_factory=dict)
    """The data problem last logged per symbol: one seen on every poll is logged once."""
    _older_deals: tuple[datetime, int, list[Deal]] | None = None
    _news: NewsCalendar | None = None
    _earnings: EarningsCalendar | None = None

    def __post_init__(self) -> None:
        schedule = self.config.schedule
        self._news = NewsCalendar(schedule.news_file, schedule.news_feed_file if schedule.news_feed else None)
        self._earnings = EarningsCalendar(schedule.earnings_file or None)

    @property
    def scope(self) -> int | None:
        """Whose trades the account limits count: every trade on the account (None), trades placed by
        hand included, or only the bot's own (its magic number). Management is always the bot's own."""
        return None if self.live and self.config.risk.account_trades else self.config.magic

    @property
    def news(self) -> NewsCalendar | None:
        return self._news

    @property
    def earnings(self) -> EarningsCalendar | None:
        return self._earnings

    def forget_account(self) -> None:
        """The terminal switched accounts: the loss limits are read again from the new account's
        history (the days before today were kept from the old one)."""
        self._older_deals = None
        self._acted_bar.clear()

    def _specs(self, positions: list[Position]) -> dict[str, SymbolSpec]:
        """Specs of the instruments held. With every trade on the account counted, that can be one
        the bot does not watch; an unavailable spec makes portfolio risk unmeasurable."""
        specs = {}
        for symbol in {p.symbol for p in positions}:
            try:
                specs[symbol] = self.broker.symbol(symbol)
            except Exception:
                log.debug("%s: no symbol spec; portfolio risk is unknown", symbol, exc_info=True)
        return specs

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
        if self.live:
            quote_problem = self.broker.tick(symbol).problem(now)
            if quote_problem:
                self._emit(now, "skip", symbol, f"data: {quote_problem}")
                return  # retry this bar when a usable quote arrives
            closed_at = _bar_close(row, self.config)
            if pd.isna(closed_at) or closed_at > now:
                self._emit(now, "skip", symbol, "data: latest bar has not closed yet")
                return
            self._data_logged.pop(symbol, None)  # the data is fine again: its next problem is logged
            if now - closed_at > timedelta(
                minutes=self.config.bar_minutes * self.config.schedule.max_bar_age_bars
            ):
                row = row.copy()
                row["signal"] = row["exit_signal"] = 0
                if "join_signal" in row:
                    row["join_signal"] = 0  # a stale bar is joined no more than it is traded
        self._manage(symbol, spec, params, row, spread, now)
        bar_time = row["time"]
        if self._acted_bar.get(symbol) == bar_time:
            return  # each bar's signal is acted on once, however often we poll
        self._acted_bar[symbol] = bar_time
        if self.live and not params.risk.auto_trade:  # signals only: the loop never enters it
            if row["signal"] != 0:
                self._signal_only(symbol, spec, params, row, spread, now)
            return
        if row["signal"] == 0:
            self._join(symbol, spec, params, row, spread, now)
            return
        reason = self.entry_blocker(symbol, spec, params, row, spread, now)
        if reason:
            self.skips[reason.split(":")[0]] += 1
            self._emit(
                now, "skip", symbol, f"{'buy' if row['signal'] > 0 else 'sell'} signal skipped: {reason}"
            )
            return
        self._enter(symbol, spec, params, row, spread, now)

    def _signal_only(
        self,
        symbol: str,
        spec: SymbolSpec,
        params: SymbolParams,
        row: pd.Series,
        spread: float,
        now: datetime,
    ) -> None:
        """A signal on a signals-only symbol (``risk.auto_trade`` off): its priced plan as a
        "signal" event for your phone, never an order. One the entry guards would have skipped
        (hours, news, loss limits, positions, spread) is logged as a skip, as for the bot's own."""
        side = 1 if row["signal"] > 0 else -1
        word = "BUY" if side > 0 else "SELL"
        reason = self.entry_blocker(symbol, spec, params, row, spread, now)
        if reason:
            self._emit(now, "skip", symbol, f"{word.lower()} signal skipped: {reason} ({SIGNALS_ONLY})")
            return
        tick = self.broker.tick(symbol)
        entry = tick.ask if side > 0 else tick.bid
        # your own trade: sized at the risk per trade, without the bot's open-risk and margin caps
        plan = self.plan_entry(symbol, spec, params, row, spread, side, caps=False)
        if isinstance(plan, Rejection):
            sl, tp = stop_distances(spec, params.risk, float(row["atr"]), spread)
            size = f"no size: {plan.message}"
        else:
            sl, tp = plan.sl_distance, plan.tp_distance
            share = plan.risk_amount / plan.equity * 100 if plan.equity > 0 else 0.0
            over = (
                f", over your {params.risk.risk_percent:g}% a trade"
                if share > params.risk.risk_percent * 1.05
                else ""
            )
            # the real share: with allow_min_lot_overrisk (gold) the smallest lot can risk far more
            size = f"{plan.volume:g} lots, {plan.risk_amount:,.2f} at the stop ({share:.1f}% risk{over})"
        digits = spec.digits
        detail = (
            f"{word} signal · signals only: the bot does not trade {symbol}; place it yourself if you want it\n"
            f"{explain(row, params.strategy)}\n"
            f"Entry ~{entry:.{digits}f} · SL {entry - side * sl:.{digits}f} · TP {entry + side * tp:.{digits}f} · {size}"
        )
        self._emit(now, "signal", symbol, detail, side=side, price=entry)

    def _join(
        self,
        symbol: str,
        spec: SymbolSpec,
        params: SymbolParams,
        row: pd.Series,
        spread: float,
        now: datetime,
    ) -> None:
        """Join a running trend after its signal (``strategy.auto_join``): once per signal, and
        never after a trade was already entered on the symbol since that signal (the signal's own
        trade, a stopped-out one, or one placed by hand)."""
        value = row.get("join_signal", 0) if params.strategy.auto_join else 0
        side = int(value) if value is not None and pd.notna(value) else 0  # NaN: a frame without joins
        signal_at = row.get("join_from")
        if not side or signal_at is None or pd.isna(signal_at):
            return
        signal_close = (
            pd.Timestamp(signal_at) + pd.Timedelta(minutes=self.config.bar_minutes)
        ).to_pydatetime()
        if any(d.entry == "in" and d.symbol == symbol and d.time >= signal_close
               for d in self.broker.deals(signal_close, self.scope)):  # fmt: skip
            return  # that signal already had its trade
        if any(p.symbol == symbol for p in self.broker.positions(self.scope)):
            return  # in the market already
        word = "buy" if side > 0 else "sell"
        reason = self.entry_blocker(symbol, spec, params, row, spread, now)
        if reason:
            self.skips[reason.split(":")[0]] += 1
            self._emit(now, "skip", symbol, f"join of the {word} signal skipped: {reason}")
            return
        self._enter(symbol, spec, params, row, spread, now, side=side,
                    why=f"joining the {word} signal of {pd.Timestamp(signal_at):%d %b %H:%M} UTC")  # fmt: skip

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
        if market_closed(params.risk.market_hours, now):
            return  # a stock before the open: the broker refuses closes and stop changes until then
        bid = self.broker.tick(symbol).bid if self.live else float(row["close"])
        bar_minutes = self.config.bar_minutes
        exit_signal = row.get("exit_signal")
        if exit_signal is None or pd.isna(exit_signal):
            exit_signal = row["signal"]  # legacy or mixed supplied backtest frames
        for position in [p for p in self.broker.positions(self.config.magic) if p.symbol == symbol]:
            price = bid if position.side == BUY else bid + spread
            in_profit = (price - position.price_open) * position.side > 0
            if (
                m.close_on_reverse
                and exit_signal == -position.side
                and _bar_close(row, self.config) > position.time_open
                and (in_profit or not m.reverse_exit_in_profit_only)
            ):
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

        # An empty setting means no kill-switch file: Path("") is the working folder, which always
        # exists, so it paused every entry for good.
        if self.live and self.config.kill_switch_file and Path(self.config.kill_switch_file).exists():
            return f"kill switch: {self.config.kill_switch_file} exists"
        if self.live:
            quote_problem = self.broker.tick(symbol).problem(now)
            if quote_problem:
                return f"data: {quote_problem}"
            closed_at = _bar_close(row, self.config)
            if pd.isna(closed_at) or closed_at > now:
                return "data: latest bar has not closed yet"
            if now - closed_at > timedelta(minutes=self.config.bar_minutes * sched.max_bar_age_bars):
                return "data: latest closed bar is stale"
        closed = sched.closed(now)
        if closed:
            return f"session: {closed}"
        shut = market_closed(r.market_hours, now)
        if shut:
            return f"market: {shut}"  # not a limit: the broker would refuse the order
        currencies = {spec.currency_base, spec.currency_profit} - {""}
        event = (
            self._news.blackout(now, currencies, sched.news_before_minutes, sched.news_after_minutes)
            if self._news
            else None
        )
        if event:
            return f"news: {event.currency} {event.title} at {event.time:%H:%M} UTC"
        report = (
            self._earnings.blackout(symbol, now, sched.earnings_days_before, sched.earnings_days_after)
            if self._earnings
            else None
        )
        if report:
            when = "around" if self._earnings and self._earnings.is_estimated(symbol, report) else "on"
            return f"earnings: {symbol} reports {when} {report:%a %d %b} (a report can gap the price past the stop)"

        status, today = self._limits(r, now, self.broker.account())
        if status.blocked:
            return status.blocked

        positions = self.broker.positions(self.scope)
        same = [p for p in positions if p.symbol == symbol]
        if len(same) >= r.max_positions_per_symbol:
            return f"positions: {len(same)} open on {symbol}"
        if len(positions) >= r.max_total_positions:
            return f"positions: {len(positions)} open in total"
        # One entry per symbol per bar, read from the deal history so it holds after a restart and
        # for a trade logged by hand. Checked after the position limits, which name the usual reason.
        if self.live and any(
            d.entry == "in" and d.symbol == symbol for d in today if d.time >= _bar_close(row, self.config)
        ):
            return f"duplicate: {symbol} was already entered on this bar; one entry per bar"

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
            older = [d for d in self.broker.deals(since, self.scope) if d.time < day_start]
            self._older_deals = (day_start, lookback, older)
        return self._older_deals[2] + self.broker.deals(day_start, self.scope)

    def risk_status(self, now: datetime) -> RiskStatus:
        """The limits plus what is open right now, for a person to read (the dashboard shows it)."""
        r = self.config.risk
        account = self.broker.account()
        status, _ = self._limits(r, now, account)
        positions = self.broker.positions(self.scope)
        committed = open_risk(positions, self._specs(positions))
        measurable = math.isfinite(committed) and math.isfinite(account.equity) and account.equity > 0
        return replace(
            status,
            open_risk_percent=committed / account.equity * 100 if measurable else None,
            open_risk_limit=r.max_total_risk_percent,
            positions=len(positions),
            positions_limit=r.max_total_positions,
            blocked=status.blocked or ("portfolio risk cannot be measured" if not measurable else ""),
        )

    def _losing_streak_today(self, deals: list[Deal], day_start: datetime) -> int:
        """Losing positions in a row (by close time). Only a streak that ends today pauses trading."""
        open_tickets = {p.ticket for p in self.broker.positions(self.scope)}
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
        caps: bool = True,
    ) -> EntryPlan | Rejection:
        """Stop/target distances, volume and portfolio checks for a new position on ``side``.

        Shared by the engine and the advisor dashboard, so the advice is the trade the bot takes.
        ``caps`` False leaves out the open-risk and margin caps (the size per trade still holds):
        for a trade you take over the bot's limits from the dashboard ("Get in").
        """
        r = params.risk
        atr_ = float(row["atr"])
        if (
            side not in (-1, 1)
            or not math.isfinite(atr_)
            or atr_ <= 0
            or not math.isfinite(spread)
            or spread < 0
        ):
            return Rejection("data", "entry requires a valid side, positive ATR and nonnegative spread")
        sl_distance, tp_distance = stop_distances(spec, r, atr_, spread)
        target = row.get("target_price")
        if target is not None and pd.notna(target) and int(row.get("raw_signal", 0)) == side:
            # the setup names its own target price (a measured move): the distance is from the fill
            tick = self.broker.tick(symbol)
            entry = tick.ask if side > 0 else tick.bid
            tp_distance = max((float(target) - entry) * side, spec.stops_level * spec.point + spread)
        if r.min_reward_risk and tp_distance < r.min_reward_risk * sl_distance - 1e-12:
            return Rejection(
                "reward",
                f"the target is {tp_distance / sl_distance:.1f} times the risk, below the minimum "
                f"{r.min_reward_risk:g}",
            )
        account = self.broker.account()
        exact = self._exact_pnl(symbol, spec, side, sl_distance, tp_distance)
        tick_value = exact[0] / (sl_distance / spec.tick_size) if exact else None
        size = position_size(spec, account.equity, sl_distance, r, tick_value)
        if not size.ok:
            return Rejection("size", size.reason)
        planned = EntryPlan(
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
        if not caps:
            return planned
        positions = self.broker.positions(self.scope)
        committed = open_risk(positions, self._specs(positions))
        if not math.isfinite(committed):  # said as it is: "would exceed 3%" hid the real reason
            stopless = [p.symbol for p in positions if not p.sl]
            return Rejection(
                "portfolio risk",
                f"an open trade has no stop loss ({', '.join(stopless) or 'unknown'}), so the open risk "
                "cannot be measured",
            )
        if (committed + size.risk_amount) / account.equity * 100 > r.max_total_risk_percent + 1e-9:
            return Rejection("portfolio risk", f"open risk would exceed {r.max_total_risk_percent}%")
        margin = self.broker.margin_required(symbol, side, size.volume)
        # Named accounts can omit leverage for sizing estimates; their broker refuses execution.
        advisory_margin = margin is None and getattr(self.broker, "advisory_only", False) is True
        if r.max_margin_percent and not advisory_margin:
            if margin is None or not math.isfinite(margin) or margin < 0:
                return Rejection("margin", "margin estimate is unavailable or invalid")
            if not math.isfinite(account.margin) or account.margin < 0:
                return Rejection("margin", "account margin is unavailable or invalid")
            projected = (account.margin + margin) / account.equity * 100
            if projected > r.max_margin_percent:
                return Rejection("margin", f"margin would reach {projected:.0f}%")
        return planned

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
        if (
            loss is None
            or win is None
            or not math.isfinite(loss)
            or not math.isfinite(win)
            or loss >= 0
            or win <= 0
        ):
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
        side: int | None = None,
        why: str = "",
    ) -> None:
        side = int(row["signal"]) if side is None else side
        plan = self.plan_entry(symbol, spec, params, row, spread, side)
        if isinstance(plan, Rejection):
            self.skips[plan.key] += 1
            self._emit(now, "skip", symbol, f"signal skipped: {plan.message}")
            return

        reason, volume = (why or plan.reason), plan.volume
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
        if result.partial and result.volume:
            volume = result.volume  # the rest was cancelled: the position is what was filled
            reason = f"{reason} · {result.message}"
        self._emit(
            now, "open", symbol, f"{word} {volume} @ {result.price} · {reason}", side, volume, result.price
        )
        sl, tp = self._levels(spec, plan, result.price) if result.price else (None, None)
        self._journal(now, "OPEN", symbol, side, volume, result.price, result.ticket, reason, sl, tp)

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
        if result.partial and result.volume:
            kind, volume = "partial", result.volume  # only part of it closed: the rest is still open
            detail = f"{detail} · {result.message}"
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
        if self.live and not self._logged_already(symbol, detail):
            log.info("%s %s", symbol, detail)

    def _logged_already(self, symbol: str, detail: str) -> bool:
        """A data problem (a stock's stale quote all night) recurs on every poll, every 15 s:
        it is logged once, and again only after the data was fine in between."""
        if not detail.startswith("data:"):
            return False
        if self._data_logged.get(symbol) == detail:
            return True
        self._data_logged[symbol] = detail
        return False

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
