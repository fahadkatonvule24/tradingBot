"""Typed configuration with validation and per-symbol overrides.

The JSON file has one section per concern (strategy, risk, management, schedule, backtest)
plus an optional ``overrides`` map keyed by symbol. Unknown keys are rejected so a typo such as
``"risk_precent"`` fails loudly instead of silently falling back to a default.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any

TIMEFRAME_MINUTES = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10, "M12": 12, "M15": 15,
    "M20": 20, "M30": 30, "H1": 60, "H2": 120, "H3": 180, "H4": 240, "H6": 360, "H8": 480,
    "H12": 720, "D1": 1440,
}  # fmt: skip

# Trading sessions in UTC hours [start, end). "asia" wraps midnight.
SESSIONS = {
    "all": (0, 24),
    "asia": (23, 8),
    "london": (7, 16),
    "new_york": (12, 21),
    "london_new_york": (7, 21),
    "overlap": (12, 16),
}

STRATEGIES = ("ema_cross", "breakout", "pullback")


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
    # pullback (RSI dips against the higher-timeframe trend, then recovers)
    pullback_rsi_period: int = 3
    pullback_buy_below: float = 15.0
    pullback_sell_above: float = 85.0
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
    trend_ema: int = 200
    use_sr_filter: bool = True
    sr_lookback: int = 80
    sr_min_atr: float = 1.0
    use_fib_filter: bool = False
    fib_lookback: int = 120
    fib_tolerance_atr: float = 0.5

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
    max_consecutive_losses: int = 4
    max_trades_per_day: int = 8
    allow_min_lot_overrisk: bool = False


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
    max_hold_bars: int = 0


@dataclass(frozen=True)
class ScheduleParams:
    session: str = "london_new_york"
    start_hour_utc: int = 0
    end_hour_utc: int = 24
    trade_weekends: bool = False
    news_file: str = "config/news_blackouts.json"
    news_before_minutes: int = 30
    news_after_minutes: int = 30
    max_bar_age_bars: int = 2

    def hours(self) -> tuple[int, int]:
        if self.session == "custom":
            return self.start_hour_utc, self.end_hour_utc
        return SESSIONS[self.session]

    def closed(self, now: datetime) -> str | None:
        """Why entries are not allowed at ``now`` (UTC) by the schedule, or None if they are."""
        if not self.trade_weekends and now.weekday() >= 5:
            return "weekend"
        start, end = self.hours()
        hour = now.hour
        inside = start <= hour < end if start < end else (hour >= start or hour < end)
        return None if inside else f"{hour:02d}:00 UTC outside {start:02d}-{end:02d}"


@dataclass(frozen=True)
class BacktestParams:
    initial_balance: float = 10_000.0
    commission_per_lot: float = 7.0
    """Round-trip commission in account currency per 1.0 lot."""
    slippage_points: float = 0.0
    min_spread_points: float = 10.0
    """Floor for the spread when bar data reports none (many demo feeds report 0)."""


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

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["symbols"] = list(self.symbols)
        data.pop("mt5")
        return data

    def validate(self) -> None:
        errors = list(_validate(self))
        for symbol in self.symbols:
            errors += [f"{symbol}: {e}" for e in _validate_symbol(self.for_symbol(symbol))]
        if errors:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))


def _base_symbol(symbol: str, overrides: dict[str, Any]) -> str:
    """Overrides are keyed by the configured name; brokers add suffixes (EURUSDm, EURUSD.r)."""
    if symbol in overrides:
        return symbol
    for key in overrides:
        if symbol.upper().startswith(key.upper()):
            return key
    return symbol


def _validate(c: BotConfig) -> list[str]:
    errors = []
    if not c.symbols:
        errors.append("at least one symbol is required")
    if c.timeframe not in TIMEFRAME_MINUTES:
        errors.append(f"timeframe {c.timeframe!r} is not one of {', '.join(TIMEFRAME_MINUTES)}")
    if c.schedule.session not in (*SESSIONS, "custom"):
        errors.append(f"schedule.session {c.schedule.session!r} is not one of {', '.join(SESSIONS)}, custom")
    start, end = c.schedule.start_hour_utc, c.schedule.end_hour_utc
    if not (0 <= start <= 23 and 1 <= end <= 24):
        errors.append("schedule hours must be start 0-23 and end 1-24 (UTC)")
    if c.history_bars < 200:
        errors.append("history_bars must be at least 200")
    unknown = set(c.overrides) - set(c.symbols)
    if unknown:
        errors.append(f"overrides for symbols that are not configured: {', '.join(sorted(unknown))}")
    return errors


def _validate_symbol(p: SymbolParams) -> list[str]:
    s, r, m = p.strategy, p.risk, p.management
    errors = []
    if s.name not in STRATEGIES:
        errors.append(f"strategy.name {s.name!r} is not one of {', '.join(STRATEGIES)}")
    if s.fast_ema >= s.slow_ema:
        errors.append("strategy.fast_ema must be lower than slow_ema")
    if s.macd_fast >= s.macd_slow:
        errors.append("strategy.macd_fast must be lower than macd_slow")
    if s.trend_timeframe not in TIMEFRAME_MINUTES:
        errors.append(f"strategy.trend_timeframe {s.trend_timeframe!r} is not a timeframe")
    if r.fixed_lot is None and not 0 < r.risk_percent <= 5:
        errors.append("risk.risk_percent must be in (0, 5]; above 5% per trade is not supported")
    if r.sl_atr <= 0 or r.tp_atr <= 0:
        errors.append("risk.sl_atr and risk.tp_atr must be positive")
    if r.max_total_risk_percent < (r.risk_percent if r.fixed_lot is None else 0):
        errors.append("risk.max_total_risk_percent is below risk_percent, so no trade could open")
    if not 0 < m.partial_tp_fraction < 1:
        errors.append("management.partial_tp_fraction must be between 0 and 1")
    return errors


def _build(cls: type, data: dict[str, Any], where: str, errors: list[str]) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        errors.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    return cls(**{k: v for k, v in data.items() if k in known})


def config_from_dict(raw: dict[str, Any], env: dict[str, str] | None = None) -> BotConfig:
    env = dict(os.environ) if env is None else env
    errors: list[str] = []
    top = {k: v for k, v in raw.items() if k not in (*_SECTIONS, "schedule", "overrides", "mt5")}
    sections = {name: _build(cls, raw.get(name, {}), name, errors) for name, cls in _SECTIONS.items()}
    schedule = _build(ScheduleParams, raw.get("schedule", {}), "schedule", errors)

    overrides: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol, parts in raw.get("overrides", {}).items():
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
        top["symbols"] = tuple(str(s) for s in top["symbols"])
    if "timeframe" in top:
        top["timeframe"] = str(top["timeframe"]).upper()
    config = _build(
        BotConfig,
        {**top, **sections, "schedule": schedule, "overrides": overrides, "mt5": mt5},
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
