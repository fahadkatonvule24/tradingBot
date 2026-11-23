"""Typed configuration with validation and per-symbol overrides.

The JSON file has one section per concern (strategy, risk, management, schedule, backtest)
plus an optional ``overrides`` map keyed by symbol. Unknown keys are rejected so a typo such as
``"risk_precent"`` fails loudly instead of silently falling back to a default.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, timezone
from datetime import time as dt_time
from functools import lru_cache
from pathlib import Path
from typing import Any, get_type_hints
from zoneinfo import ZoneInfo

TIMEFRAME_MINUTES = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10, "M12": 12, "M15": 15,
    "M20": 20, "M30": 30, "H1": 60, "H2": 120, "H3": 180, "H4": 240, "H6": 360, "H8": 480,
    "H12": 720, "D1": 1440,
}  # fmt: skip

# Trading sessions in UTC hours [start, end) while London and New York are on summer time. "asia"
# wraps midnight. The four below that open or close in London or New York move with those cities'
# clocks: use session_hours() for the hours on a given day.
SESSIONS = {
    "all": (0, 24),
    "asia": (23, 8),
    "london": (7, 16),
    "new_york": (12, 21),
    "london_new_york": (7, 21),
    "overlap": (12, 16),
}

LONDON, NEW_YORK = "Europe/London", "America/New_York"
SESSION_CLOCKS = {
    "london": ((LONDON, 8), (LONDON, 17)),
    "new_york": ((NEW_YORK, 8), (NEW_YORK, 17)),
    "london_new_york": ((LONDON, 8), (NEW_YORK, 17)),
    "overlap": ((NEW_YORK, 8), (LONDON, 17)),
}
"""Open and close on the city's own clock (08:00 to 17:00 local). In UTC they are an hour later
in winter: London opens at 07:00 UTC in summer and 08:00 UTC from late October to late March.
Fixed UTC hours put the first hour of the session before London had opened for five months a
year. The weeks when only one of the two cities has changed its clock are handled too."""
CITY = {LONDON: "London", NEW_YORK: "New York"}


@lru_cache(maxsize=8192)
def _session_hours(name: str, day: date) -> tuple[int, int]:
    clocks = SESSION_CLOCKS.get(name)
    if clocks is None:
        return SESSIONS[name]
    hours = [
        datetime.combine(day, dt_time(hour), ZoneInfo(zone)).astimezone(timezone.utc).hour
        for zone, hour in clocks
    ]
    return hours[0], hours[1]


def session_hours(name: str, when: datetime | date | None = None) -> tuple[int, int]:
    """UTC hours [start, end) of session ``name`` on the UTC day of ``when`` (today if None).

    ``when`` may be naive (taken as UTC) or carry a time zone.
    """
    if when is None:
        day = datetime.now(timezone.utc).date()
    elif isinstance(when, datetime):
        day = (when.astimezone(timezone.utc) if when.tzinfo else when).date()
    else:
        day = when
    return _session_hours(name, day)


def session_clock_text(name: str) -> str:
    """The session in the local time it follows, e.g. "London 08:00 to New York 17:00, local time"."""
    clocks = SESSION_CLOCKS.get(name)
    if clocks is None:
        return ""
    (open_zone, open_hour), (close_zone, close_hour) = clocks
    if open_zone == close_zone:
        return f"{open_hour:02d}:00 to {close_hour:02d}:00 {CITY[open_zone]} time"
    return f"{CITY[open_zone]} {open_hour:02d}:00 to {CITY[close_zone]} {close_hour:02d}:00, local time"


MARKET_HOURS = {
    "us_stocks": ("America/New_York", dt_time(9, 30), dt_time(16, 0), "09:30 to 16:00 New York time")
}
"""Exchange hours by name: (time zone, open, close, as said to a person). Weekdays only."""


US_HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
))  # fmt: skip
"""NYSE full closures (New York dates) from the stocks' addition to the end of 2027. After that,
only weekends are known: extend this each year from the NYSE calendar."""
US_EARLY_CLOSES = frozenset(date.fromisoformat(d) for d in ("2026-11-27", "2026-12-24", "2027-11-26"))
"""NYSE days that close at 13:00 New York time."""


def market_closed(name: str, now: datetime) -> str | None:
    """Why the exchange ``name`` takes no orders at ``now`` (naive UTC), or None when it does (or
    when ``name`` is empty: the instrument trades whenever the broker quotes it)."""
    if not name:
        return None
    zone, opens, closes, text = MARKET_HOURS[name]
    local = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo(zone))
    if name == "us_stocks":
        if local.date() in US_HOLIDAYS:
            return "the exchange is closed for a US holiday"
        if local.date() in US_EARLY_CLOSES:
            closes = dt_time(13, 0)
            text = f"{text}, and only to 13:00 today"
    if local.weekday() >= 5 or not opens <= local.time() < closes:
        return f"the exchange is closed (it takes orders {text}, weekdays)"
    return None


STRATEGIES = ("ema_cross", "breakout", "pullback", "retest")

# Top-down analysis: the two higher timeframes that set the direction for each trading timeframe.
# Each step is roughly 4-12x the one below it, the usual spacing for multi-timeframe analysis.
AUTO_TREND_TIMEFRAMES = {
    "M1": ("M15", "H1"),
    "M5": ("H1", "H4"),
    "M15": ("H1", "H4"),
    "M30": ("H1", "H4"),
    "H1": ("H4", "D1"),
    "H4": ("D1",),
    "D1": (),
}
_STANDARD = ("M5", "M15", "M30", "H1", "H4", "D1")


def auto_trend_timeframes(timeframe: str) -> tuple[str, ...]:
    if timeframe in AUTO_TREND_TIMEFRAMES:
        return AUTO_TREND_TIMEFRAMES[timeframe]
    base = TIMEFRAME_MINUTES[timeframe]  # non-standard timeframes: the next two at least 3x larger
    return tuple(tf for tf in _STANDARD if TIMEFRAME_MINUTES[tf] >= 3 * base)[:2]


class ConfigError(ValueError):
    """Raised for invalid configuration, listing every problem found."""


@dataclass(frozen=True)
class StrategyParams:
    name: str = "ema_cross"
    # ema_cross
    fast_ema: int = 10
    slow_ema: int = 50
    rsi_period: int = 14
    rsi_buy_max: float = 68.0
    rsi_sell_min: float = 32.0
    # breakout
    breakout_period: int = 20
    breakout_min_atr: float = 0.25
    """A break only counts if the bar closes at least this many ATR beyond the level, and it has
    failed if a later bar closes back through the level by the same margin. 0 = any close through."""
    auto_join: bool = False
    """Also enter a running trend after its signal, as the dashboard's JOIN: within
    ``join_window_bars`` bars of a taken signal, the trend unchanged, no opposite supply/demand zone
    and no closed sweep against it, and only if no trade was entered on the symbol since that
    signal. Late entries up to 5 bars did about as well as on time (``scripts/research_late_entry.py``)."""
    join_window_bars: int = 5
    breakout_target: str = "atr"
    """"atr": the target is ``risk.tp_atr`` ATR, as for every setup. "measured": the measured move,
    the height of the range that broke projected from the broken level."""
    use_volume_filter: bool = False
    """Breakout only: the break bar's tick volume must be ``volume_min_ratio`` times the average of
    the previous ``volume_period`` bars. Needs tick volume in the bars (``tradebot fetch`` saves it)."""
    volume_period: int = 20
    volume_min_ratio: float = 1.5
    # pullback (RSI dips against the higher-timeframe trend, then recovers)
    pullback_rsi_period: int = 3
    pullback_buy_below: float = 15.0
    pullback_sell_above: float = 85.0
    # retest (a bar closes through the prior N-bar high/low, then price comes back to that level
    # within ``retest_window`` bars and a bar closes away from it again)
    retest_period: int = 20
    retest_window: int = 12
    retest_tolerance_atr: float = 0.25
    # shared filters
    atr_period: int = 14
    use_adx: bool = True
    adx_period: int = 14
    adx_min: float = 20.0
    use_macd: bool = True
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    use_trend_filter: bool = True
    trend_timeframe: str = "H4"
    trend_timeframes: tuple[str, ...] | str = ()
    """Higher timeframes that must all agree with a trade: a list such as ["H1", "H4"], or "auto" to
    pick the two that suit the trading timeframe. Empty = the single ``trend_timeframe``."""
    trend_ema: int = 200
    use_sr_filter: bool = True
    sr_lookback: int = 80
    sr_min_atr: float = 1.0
    use_fib_filter: bool = False
    fib_lookback: int = 120
    fib_tolerance_atr: float = 0.5
    # Entry-quality heuristics. Opt in explicitly so historical strategy configs keep their rules.
    use_signal_quality_filter: bool = False
    quality_period: int = 20
    quality_min_efficiency: float = 0.2
    quality_min_body_ratio: float = 0.2
    quality_min_close_position: float = 0.65
    quality_max_range_atr: float = 2.5
    quality_max_extension_atr: float = 2.0

    def trend_ladder(self, timeframe: str) -> tuple[str, ...]:
        """The higher timeframes the trend filter reads when trading ``timeframe``."""
        if self.trend_timeframes == "auto":
            return auto_trend_timeframes(timeframe)
        if isinstance(self.trend_timeframes, str):  # "H4": one timeframe, not the letters H and 4
            return (self.trend_timeframes,)
        if self.trend_timeframes:
            return tuple(self.trend_timeframes)
        return (self.trend_timeframe,)

    @property
    def warmup_bars(self) -> int:
        return (
            max(
                self.slow_ema,
                self.rsi_period,
                self.atr_period,
                self.adx_period * 3,
                self.macd_slow + self.macd_signal,
                self.sr_lookback,
                self.fib_lookback,
                self.breakout_period,
                self.retest_period,
                self.volume_period + 1 if self.use_volume_filter else 0,
                self.pullback_rsi_period if self.name == "pullback" else 0,
                self.quality_period + 1 if self.use_signal_quality_filter else 0,
            )
            + 5
        )


@dataclass(frozen=True)
class RiskParams:
    risk_percent: float = 0.25
    fixed_lot: float | None = None
    sl_atr: float = 1.5
    tp_atr: float = 3.0
    max_spread_atr: float = 0.25
    max_positions_per_symbol: int = 1
    max_total_positions: int = 3
    max_total_risk_percent: float = 1.0
    max_margin_percent: float = 30.0
    daily_loss_limit_percent: float = 2.0
    weekly_loss_limit_percent: float = 5.0
    max_drawdown_percent: float = 10.0
    """Live only: no new entries while the bot's realised result is this far below its highest
    point of the last ``drawdown_days`` days. 0 = off. Backtests do not apply it, so a backtest
    shows the full drawdown of the rules; ``tradebot backtest`` says when it would have tripped."""
    drawdown_days: int = 365
    max_consecutive_losses: int = 4
    max_trades_per_day: int = 8
    min_reward_risk: float = 0.0
    """Skip a trade whose target is closer than this many times its stop distance. 0 = off. Only
    matters when a setup names its own target (the measured move); ATR targets are a fixed ratio."""
    allow_min_lot_overrisk: bool = False
    market_hours: str = ""
    """When the instrument's exchange takes orders, beyond the global trading hours: "" = whenever
    the broker quotes it; "us_stocks" = 09:30 to 16:00 New York time on weekdays. Exness quotes US
    stocks, and builds their bars, from 06:00 New York, but refuses orders before the open: an entry
    on a pre-market bar was rejected live and counted as filled in backtests."""
    auto_trade: bool = True
    """False = signals only: the trading loop never opens a trade on this symbol by itself (no
    signal, no join), but still manages one that is open. The dashboard shows its advice and
    alerts as usual and you place what you want; backtests measure it like any other symbol.
    Meant as a per-symbol override, e.g. ``"TSLA": {"risk": {"auto_trade": false}}``."""
    account_trades: bool = True
    """Live: the loss limits, losing streak, position limits, open risk and review count every
    trade on the account, the ones you place by hand (on the phone, too) as well as the bot's.
    False = only the bot's own (its magic number). The trading loop only ever manages its own
    positions; the dashboard's ``management.auto_*`` switches act on the others."""


@dataclass(frozen=True)
class ManagementParams:
    breakeven_r: float = 1.0
    breakeven_buffer_r: float = 0.1
    trail_start_r: float = 1.5
    trail_atr: float = 2.0
    partial_tp: bool = True
    partial_tp_r: float = 1.5
    partial_tp_fraction: float = 0.5
    close_on_reverse: bool = True
    reverse_exit_in_profit_only: bool = False
    """With ``close_on_reverse``: close on an opposite signal only while the trade is in profit,
    leaving a losing trade to its stop. Measured in ``scripts/research_trailing.py``."""
    max_hold_bars: int = 0
    auto_breakeven: bool = False
    """Move the stop of every open trade on the account to breakeven at ``breakeven_r``, the ones
    placed by hand included (see ``tradebot.protect``). Changes stops only; never opens a trade."""
    auto_trailing: bool = False
    """From ``trail_start_r`` in profit, keep the stop of every open trade on the account
    ``trail_atr`` ATR behind the last closed bar, the ones placed by hand included (see
    ``tradebot.protect``). Only ever tightens a stop. Measured neutral (scripts/research_trailing.py)."""
    auto_partial: bool = False
    """Close ``partial_tp_fraction`` of every open trade on the account once a closed bar is
    ``partial_tp_r`` R in profit, the ones placed by hand included. Closes part of a trade."""
    auto_reverse_exit: bool = False
    """Close an open trade on the account, the ones placed by hand included, when the reversal check
    confirms a sign against it on a closed bar while the trade is in profit (at that close and at the
    live price). Closes the whole trade. Measured in ``scripts/research_reversal_exit.py``."""
    auto_stop: bool = False
    """Give an open trade on the account that has had no stop for two minutes one at ``risk.sl_atr``
    ATR from its entry (from the price, when price is already past that). Never removes or widens
    a stop."""


@dataclass(frozen=True)
class ScheduleParams:
    session: str = "london_new_york"
    start_hour_utc: int = 0
    end_hour_utc: int = 24
    trade_weekends: bool = False
    news_file: str = "config/news_blackouts.json"
    """Your own high-impact events, for anything the feed does not carry."""
    news_feed: str = ""
    """URL of a weekly calendar in the ForexFactory JSON format, downloaded every few hours by the
    dashboard and the trading loop. Empty = off; config.json turns it on."""
    news_feed_file: str = "news_feed.json"
    news_before_minutes: int = 30
    news_after_minutes: int = 30
    earnings_file: str = "config/earnings.json"
    """Stocks' earnings report dates (``tradebot.earnings``): no new entry on a stock from
    ``earnings_days_before`` trading days before its report to ``earnings_days_after`` after it.
    Empty, missing or without the stock = no earnings pause for it."""
    earnings_days_before: int = 1
    earnings_days_after: int = 1
    max_bar_age_bars: int = 2

    def hours(self, when: datetime | date | None = None) -> tuple[int, int]:
        """UTC hours [start, end) on the day of ``when`` (today if None): London and New York hours
        move with daylight saving, a custom window does not."""
        if self.session == "custom":
            return self.start_hour_utc, self.end_hour_utc
        return session_hours(self.session, when)

    def closed(self, now: datetime) -> str | None:
        """Why entries are not allowed at ``now`` (UTC) by the schedule, or None if they are."""
        if not self.trade_weekends and now.weekday() >= 5:
            return "weekend"
        start, end = self.hours(now)
        hour = now.hour
        inside = start <= hour < end if start < end else (hour >= start or hour < end)
        return None if inside else f"{hour:02d}:00 UTC outside {start:02d}-{end:02d}"


ALERT_LEVELS = ("important", "all")


@dataclass(frozen=True)
class AlertParams:
    """What reaches your phone. The dashboard shows the other setups' tiles either way."""

    other_setups: bool = False
    """Also announce signals from the three setups that are not on screen. Off by default: every
    setup loses on the stored history, and four of them send about four times the signals."""
    whatsapp: str = "important"
    """Which alerts go to WhatsApp: "important" (confirmed buy and sell signals, failed breaks,
    get-out and reversal warnings on open positions, news, orders) or "all" (also the unconfirmed
    forming triggers, blocked and skipped triggers, trend and analysis changes)."""
    ntfy: str = "important"
    """The same choice for the ntfy app. ntfy.sh allows 250 messages a day per sender; with "all"
    the extra kinds arrive silently, in the notification drawer only."""


@dataclass(frozen=True)
class BacktestParams:
    initial_balance: float = 10_000.0
    commission_per_lot: float = 7.0
    """Round-trip commission in account currency per 1.0 lot."""
    slippage_points: float = 0.0
    min_spread_points: float = 10.0
    """Floor for the spread when bar data reports none (many demo feeds report 0)."""
    swap_long: float = 0.0
    swap_short: float = 0.0
    """Overnight financing per lot per night in account currency, as the broker charges it
    (negative = a cost): one night per 17:00 New York rollover crossed, three on ``swap_triple_day``.
    0 = not simulated. MT5 shows the rates in the symbol's specification."""
    swap_triple_day: int = 2
    """The rollover charged three times, Monday = 0: Wednesday (2) for forex and metals, Friday (4)
    for stocks at Exness."""


@dataclass(frozen=True)
class SymbolParams:
    """Everything that may differ per symbol, resolved after applying overrides."""

    strategy: StrategyParams
    risk: RiskParams
    management: ManagementParams
    backtest: BacktestParams


@dataclass(frozen=True)
class MT5Login:
    path: str | None = None
    login: int | None = None
    password: str | None = field(default=None, repr=False)
    server: str | None = None


_SECTIONS = {
    "strategy": StrategyParams,
    "risk": RiskParams,
    "management": ManagementParams,
    "backtest": BacktestParams,
}


@dataclass(frozen=True)
class BotConfig:
    symbols: tuple[str, ...] = ("EURUSD",)
    timeframe: str = "M15"
    history_bars: int = 1500
    poll_seconds: int = 15
    magic: int = 230504
    comment: str = "tradebot"
    dry_run: bool = True
    live_trading_confirm: bool = False
    allow_real_account: bool = False
    kill_switch_file: str = "STOP"
    journal: str = "trade_journal.sqlite"
    server_utc_offset_hours: float | None = None
    """Broker server time minus UTC. None = detect from a live tick."""
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    management: ManagementParams = field(default_factory=ManagementParams)
    schedule: ScheduleParams = field(default_factory=ScheduleParams)
    backtest: BacktestParams = field(default_factory=BacktestParams)
    alerts: AlertParams = field(default_factory=AlertParams)
    overrides: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    mt5: MT5Login = field(default_factory=MT5Login)

    @property
    def bar_minutes(self) -> int:
        return TIMEFRAME_MINUTES[self.timeframe]

    @property
    def live_enabled(self) -> bool:
        return not self.dry_run and self.live_trading_confirm

    def for_symbol(self, symbol: str) -> SymbolParams:
        sections = {name: getattr(self, name) for name in _SECTIONS}
        for name, values in self.overrides.get(_base_symbol(symbol, self.overrides), {}).items():
            sections[name] = replace(sections[name], **values)
        return SymbolParams(**sections)

    def with_strategy(self, **changes: Any) -> BotConfig:
        return replace(self, strategy=replace(self.strategy, **changes))

    def with_setup(self, name: str) -> BotConfig:
        """This config with ``name`` as the setup of every symbol. A per-symbol setup name would
        otherwise silently keep that symbol on its own setup; its other overrides are kept."""
        overrides = {
            symbol: {
                section: {k: v for k, v in values.items() if not (section == "strategy" and k == "name")}
                for section, values in sections.items()
            }
            for symbol, sections in self.overrides.items()
        }
        return replace(self.with_strategy(name=name), overrides=overrides)

    def setups(self) -> dict[str, str]:
        """The setup each symbol trades, after its overrides."""
        return {symbol: self.for_symbol(symbol).strategy.name for symbol in self.symbols}

    def setup_groups(self) -> list[tuple[str, list[str]]]:
        """(setup, symbols) pairs, the config's own setup first: one pair unless symbols differ."""
        groups: dict[str, list[str]] = {self.strategy.name: []}
        for symbol, name in self.setups().items():
            groups.setdefault(name, []).append(symbol)
        return [(name, symbols) for name, symbols in groups.items() if symbols]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["symbols"] = list(self.symbols)
        data.pop("mt5")
        return data

    def validate(self) -> None:
        errors = list(_validate(self))
        for symbol in self.symbols:
            errors += [f"{symbol}: {e}" for e in _validate_symbol(self.for_symbol(symbol))]
        if not errors:
            for symbol in self.symbols:
                strategy = self.for_symbol(symbol).strategy
                required = strategy.warmup_bars
                if strategy.use_trend_filter and any(
                    TIMEFRAME_MINUTES[tf] <= self.bar_minutes for tf in strategy.trend_ladder(self.timeframe)
                ):
                    required = max(required, strategy.trend_ema)
                if self.history_bars < required:
                    errors.append(
                        f"{symbol}: history_bars must be at least {required} for its indicator periods"
                    )
            empty = [
                s for s in self.symbols if _no_trend_timeframe(self.for_symbol(s).strategy, self.timeframe)
            ]
            if empty:
                errors.append(
                    f'strategy.use_trend_filter is on, but trend_timeframes "auto" has no timeframe above '
                    f"{self.timeframe} to read the trend from, so every entry would be blocked "
                    f"({', '.join(empty)}): set use_trend_filter to false, or trade a lower timeframe"
                )
        if errors:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))


def _no_trend_timeframe(s: StrategyParams, timeframe: str) -> bool:
    """The trend filter is on but its automatic ladder is empty (D1 has nothing above it): every
    setup needs the trend to agree, so it would never trade."""
    return bool(s.use_trend_filter and s.trend_timeframes == "auto" and timeframe in TIMEFRAME_MINUTES
                and not s.trend_ladder(timeframe))  # fmt: skip


def _base_symbol(symbol: str, overrides: dict[str, Any]) -> str:
    """Overrides are keyed by the configured name; brokers add suffixes (EURUSDm, EURUSD.r)."""
    if symbol in overrides:
        return symbol
    # The longest matching key: with "T" and "TSLA" both configured, TSLAm is TSLA, not T.
    matches = [key for key in overrides if symbol.upper().startswith(key.upper())]
    return max(matches, key=len) if matches else symbol


def _validate(c: BotConfig) -> list[str]:
    errors = _scalar_errors(c, "config") + _scalar_errors(c.schedule, "schedule")
    errors += _scalar_errors(c.alerts, "alerts")
    if errors:
        return errors
    if not c.symbols:
        errors.append("at least one symbol is required")
    if any(s != s.strip() for s in c.symbols):
        errors.append("symbols must not contain leading or trailing whitespace")
    if len({s.upper() for s in c.symbols}) != len(c.symbols):
        errors.append("symbols must be unique (ignoring case)")
    if c.timeframe not in TIMEFRAME_MINUTES:
        errors.append(f"timeframe {c.timeframe!r} is not one of {', '.join(TIMEFRAME_MINUTES)}")
    if c.schedule.session not in (*SESSIONS, "custom"):
        errors.append(f"schedule.session {c.schedule.session!r} is not one of {', '.join(SESSIONS)}, custom")
    start, end = c.schedule.start_hour_utc, c.schedule.end_hour_utc
    if not (0 <= start <= 23 and 1 <= end <= 24):
        errors.append("schedule hours must be start 0-23 and end 1-24 (UTC)")
    elif c.schedule.session == "custom" and start == end:  # would read as open around the clock
        errors.append("schedule start_hour_utc and end_hour_utc are equal: use 0 and 24 for all day")
    if c.history_bars < 200:
        errors.append("history_bars must be at least 200")
    if c.poll_seconds <= 0:
        errors.append("poll_seconds must be positive")
    if c.server_utc_offset_hours is not None and abs(c.server_utc_offset_hours) > 14:
        errors.append("server_utc_offset_hours must be between -14 and 14, or null for detection")
    if c.schedule.max_bar_age_bars < 1:
        errors.append("schedule.max_bar_age_bars must be >= 1")
    if c.schedule.news_before_minutes < 0 or c.schedule.news_after_minutes < 0:
        errors.append("schedule.news_before_minutes and news_after_minutes must be >= 0")
    if not 0 <= c.schedule.earnings_days_before <= 10 or not 0 <= c.schedule.earnings_days_after <= 10:
        errors.append("schedule.earnings_days_before and earnings_days_after must be 0 to 10")
    for channel in ("whatsapp", "ntfy"):
        level = getattr(c.alerts, channel)
        if level not in ALERT_LEVELS:
            errors.append(f"alerts.{channel} {level!r} is not one of {', '.join(ALERT_LEVELS)}")
    if not isinstance(c.alerts.other_setups, bool):
        errors.append("alerts.other_setups must be true or false")
    unknown = set(c.overrides) - set(c.symbols)
    if unknown:
        errors.append(f"overrides for symbols that are not configured: {', '.join(sorted(unknown))}")
    return errors


def _validate_symbol(p: SymbolParams) -> list[str]:
    s, r, m = p.strategy, p.risk, p.management
    errors = [error for name in _SECTIONS for error in _scalar_errors(getattr(p, name), name)]
    if errors:
        return errors
    for name in (
        "fast_ema",
        "slow_ema",
        "rsi_period",
        "atr_period",
        "adx_period",
        "macd_fast",
        "macd_slow",
        "macd_signal",
        "trend_ema",
        "sr_lookback",
        "fib_lookback",
        "breakout_period",
        "pullback_rsi_period",
    ):
        if getattr(s, name) < 1:
            errors.append(f"strategy.{name} must be a positive integer")
    for name in (
        "max_spread_atr",
        "max_margin_percent",
        "daily_loss_limit_percent",
        "weekly_loss_limit_percent",
        "max_consecutive_losses",
        "max_trades_per_day",
        "max_positions_per_symbol",
        "max_total_positions",
    ):
        if getattr(r, name) < 0:
            errors.append(f"risk.{name} must be >= 0")
    if r.fixed_lot is not None and r.fixed_lot <= 0:
        errors.append("risk.fixed_lot must be positive or null")
    for name in ("breakeven_r", "breakeven_buffer_r", "trail_start_r", "max_hold_bars"):
        if getattr(m, name) < 0:
            errors.append(f"management.{name} must be >= 0")
    if m.trail_atr <= 0 or m.partial_tp_r <= 0:
        errors.append("management.trail_atr and partial_tp_r must be positive")
    if p.backtest.initial_balance <= 0:
        errors.append("backtest.initial_balance must be positive")
    for name in ("commission_per_lot", "slippage_points", "min_spread_points"):
        if getattr(p.backtest, name) < 0:
            errors.append(f"backtest.{name} must be >= 0")
    if p.risk.market_hours not in ("", *MARKET_HOURS):
        errors.append(f"risk.market_hours must be empty or one of: {', '.join(MARKET_HOURS)}")
    if not 0 <= p.backtest.swap_triple_day <= 6:
        errors.append("backtest.swap_triple_day must be 0 (Monday) to 6 (Sunday)")
    if s.name not in STRATEGIES:
        errors.append(f"strategy.name {s.name!r} is not one of {', '.join(STRATEGIES)}")
    if s.breakout_min_atr < 0:
        errors.append("strategy.breakout_min_atr must be >= 0")
    for name in ("rsi_buy_max", "rsi_sell_min", "pullback_buy_below", "pullback_sell_above", "adx_min"):
        if not 0 <= getattr(s, name) <= 100:
            errors.append(f"strategy.{name} must be between 0 and 100")
    for name in ("sr_min_atr", "fib_tolerance_atr"):
        if getattr(s, name) < 0:
            errors.append(f"strategy.{name} must be >= 0")
    if not 1 <= s.join_window_bars <= 12:  # the dashboard looks 12 bars back for the signal to join
        errors.append("strategy.join_window_bars must be from 1 to 12")
    if s.retest_period < 2 or s.retest_window < 1 or s.retest_tolerance_atr < 0:
        errors.append("strategy.retest_period must be >= 2, retest_window >= 1, retest_tolerance_atr >= 0")
    if s.fast_ema >= s.slow_ema:
        errors.append("strategy.fast_ema must be lower than slow_ema")
    if s.macd_fast >= s.macd_slow:
        errors.append("strategy.macd_fast must be lower than macd_slow")
    if s.trend_timeframe not in TIMEFRAME_MINUTES:
        errors.append(f"strategy.trend_timeframe {s.trend_timeframe!r} is not a timeframe")
    if not isinstance(s.trend_timeframes, (str, list, tuple)):
        errors.append('strategy.trend_timeframes must be "auto" or a list of timeframes')
    elif s.trend_timeframes != "auto":
        bad = [t for t in ([s.trend_timeframes] if isinstance(s.trend_timeframes, str) else s.trend_timeframes)
               if not isinstance(t, str) or t not in TIMEFRAME_MINUTES]  # fmt: skip
        if bad:
            errors.append(f'strategy.trend_timeframes must be "auto" or a list of timeframes; got {bad}')
    if r.fixed_lot is None and not 0 < r.risk_percent <= 5:
        errors.append("risk.risk_percent must be in (0, 5]; above 5% per trade is not supported")
    if s.breakout_target not in ("atr", "measured"):
        errors.append('strategy.breakout_target must be "atr" or "measured"')
    if s.volume_period < 2 or s.volume_min_ratio <= 0:
        errors.append("strategy.volume_period must be >= 2 and volume_min_ratio positive")
    if not isinstance(s.use_signal_quality_filter, bool):
        errors.append("strategy.use_signal_quality_filter must be a boolean")
    if type(s.quality_period) is not int or s.quality_period < 2:
        errors.append("strategy.quality_period must be an integer >= 2")
    for name, minimum, maximum in (
        ("quality_min_efficiency", 0, 1),
        ("quality_min_body_ratio", 0, 1),
        ("quality_min_close_position", 0.5, 1),
        ("quality_max_range_atr", 0, None),
        ("quality_max_extension_atr", 0, None),
    ):
        value = getattr(s, name)
        finite = type(value) in (int, float) and math.isfinite(value)
        if not finite or (not minimum < value if maximum is None else not minimum <= value <= maximum):
            bounds = "positive and finite" if maximum is None else f"finite and in [{minimum}, {maximum}]"
            errors.append(f"strategy.{name} must be {bounds}")
    measured = s.name == "breakout" and s.breakout_target == "measured"
    if r.min_reward_risk < 0 or (
        not measured and r.sl_atr > 0 and r.min_reward_risk > r.tp_atr / r.sl_atr + 1e-9
    ):
        errors.append("risk.min_reward_risk is above tp_atr / sl_atr, so no trade could open")
    if r.sl_atr <= 0 or r.tp_atr <= 0:
        errors.append("risk.sl_atr and risk.tp_atr must be positive")
    if r.max_total_risk_percent < (r.risk_percent if r.fixed_lot is None else 0):
        errors.append("risk.max_total_risk_percent is below risk_percent, so no trade could open")
    if r.max_drawdown_percent < 0 or r.drawdown_days < 1:
        errors.append("risk.max_drawdown_percent must be >= 0 and risk.drawdown_days >= 1")
    if not 0 < m.partial_tp_fraction < 1:
        errors.append("management.partial_tp_fraction must be between 0 and 1")
    return errors


def _scalar_errors(section: Any, where: str) -> list[str]:
    """Dataclass annotations do not validate JSON: reject truthy strings and NaN before use."""
    errors = []
    for name, hint in get_type_hints(type(section)).items():
        value = getattr(section, name)
        if hint in (float | None, int | None, str | None):
            if value is None:
                continue
            hint = next(t for t in hint.__args__ if t is not type(None))
        if hint is bool and type(value) is not bool:
            errors.append(f"{where}.{name} must be a boolean")
        elif hint is int and type(value) is not int:
            errors.append(f"{where}.{name} must be an integer")
        elif hint is float:
            try:
                valid = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                errors.append(f"{where}.{name} must be a finite number")
        elif hint is str and not isinstance(value, str):
            errors.append(f"{where}.{name} must be a string")
    return errors


def _build(cls: type, data: dict[str, Any], where: str, errors: list[str]) -> Any:
    if not isinstance(data, dict):
        errors.append(f"{where} must be an object")
        return cls()
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        errors.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    return cls(**{k: v for k, v in data.items() if k in known})


def config_from_dict(raw: dict[str, Any], env: dict[str, str] | None = None) -> BotConfig:
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be an object")
    for name in (*_SECTIONS, "schedule", "alerts", "overrides", "mt5"):
        if name in raw and not isinstance(raw[name], dict):
            raise ConfigError(f"{name} must be an object")
    env = dict(os.environ) if env is None else env
    errors: list[str] = []
    top = {k: v for k, v in raw.items() if k not in (*_SECTIONS, "schedule", "alerts", "overrides", "mt5")}
    sections = {name: _build(cls, raw.get(name, {}), name, errors) for name, cls in _SECTIONS.items()}
    schedule = _build(ScheduleParams, raw.get("schedule", {}), "schedule", errors)
    alerts = _build(AlertParams, raw.get("alerts", {}), "alerts", errors)

    overrides: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol, parts in raw.get("overrides", {}).items():
        if not isinstance(symbol, str) or not isinstance(parts, dict):
            raise ConfigError("overrides must map symbol names to section objects")
        overrides[symbol] = {}
        for name, values in parts.items():
            if name not in _SECTIONS:
                errors.append(f"overrides.{symbol}: unknown section {name!r}")
                continue
            _build(_SECTIONS[name], values, f"overrides.{symbol}.{name}", errors)
            overrides[symbol][name] = values

    mt5_raw = raw.get("mt5", {})
    login = env.get("MT5_LOGIN") or mt5_raw.get("login")
    mt5 = MT5Login(
        path=env.get("MT5_PATH") or mt5_raw.get("path"),
        login=int(login) if login else None,
        password=env.get("MT5_PASSWORD") or mt5_raw.get("password"),
        server=env.get("MT5_SERVER") or mt5_raw.get("server"),
    )
    if "symbols" in top:
        symbols = top["symbols"]
        if not isinstance(symbols, (list, tuple)) or any(
            not isinstance(s, str) or not s.strip() for s in symbols
        ):
            raise ConfigError("symbols must be a list of nonempty symbol names")
        top["symbols"] = tuple(symbols)
    if "timeframe" in top:
        top["timeframe"] = str(top["timeframe"]).upper()
    config = _build(
        BotConfig,
        {**top, **sections, "schedule": schedule, "alerts": alerts, "overrides": overrides, "mt5": mt5},
        "config",
        errors,
    )
    if errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
    config.validate()
    return config


def load_config(path: str | Path, env: dict[str, str] | None = None) -> BotConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from None
    _load_dotenv(path.parent / ".env", env)
    _load_dotenv(Path(".env"), env)
    return config_from_dict(raw, env)


def _load_dotenv(path: Path, env: dict[str, str] | None) -> None:
    """Minimal .env support (KEY=VALUE lines) so credentials never live in config.json."""
    if env is not None or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
