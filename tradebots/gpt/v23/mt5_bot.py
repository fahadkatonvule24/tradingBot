from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import MetaTrader5 as mt5
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: MetaTrader5. Install it with `pip install -r requirements.txt`."
    ) from exc


TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M2": mt5.TIMEFRAME_M2,
    "M3": mt5.TIMEFRAME_M3,
    "M4": mt5.TIMEFRAME_M4,
    "M5": mt5.TIMEFRAME_M5,
    "M6": mt5.TIMEFRAME_M6,
    "M10": mt5.TIMEFRAME_M10,
    "M12": mt5.TIMEFRAME_M12,
    "M15": mt5.TIMEFRAME_M15,
    "M20": mt5.TIMEFRAME_M20,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2,
    "H3": mt5.TIMEFRAME_H3,
    "H4": mt5.TIMEFRAME_H4,
    "H6": mt5.TIMEFRAME_H6,
    "H8": mt5.TIMEFRAME_H8,
    "H12": mt5.TIMEFRAME_H12,
    "D1": mt5.TIMEFRAME_D1,
}

# Tracks tickets for which partial TP was already taken this session
_partial_tp_taken: set[int] = set()


@dataclass
class BotConfig:
    symbol: str
    symbols: list[str]
    timeframe: str
    bars: int
    poll_seconds: int
    max_bar_age_minutes: int
    fast_ema: int
    slow_ema: int
    trend_timeframe: str
    trend_ema: int
    rsi_period: int
    buy_rsi_max: float
    sell_rsi_min: float
    atr_period: int
    use_atr_stops: bool
    atr_sl_multiplier: float
    atr_tp_multiplier: float
    min_atr_pips: float
    # ADX regime filter
    use_adx_filter: bool
    adx_period: int
    adx_min_threshold: float
    # MACD confirmation
    use_macd_confirm: bool
    macd_fast: int
    macd_slow: int
    macd_signal_period: int
    risk_percent: float
    fixed_lot: Optional[float]
    stop_loss_pips: float
    take_profit_pips: float
    max_spread_pips: float
    max_positions: int
    max_total_positions: int
    max_total_risk_percent: float
    max_margin_percent: float
    daily_loss_limit_percent: float
    weekly_loss_limit_percent: float
    max_consecutive_losses: int
    trailing_stop_pips: float
    breakeven_after_pips: float
    # Partial take-profit
    partial_tp_enabled: bool
    partial_tp_percent: float
    partial_tp_multiplier: float
    trade_start_hour: int
    trade_end_hour: int
    session_preset: str
    use_support_resistance: bool
    sr_lookback_bars: int
    sr_min_distance_pips: float
    use_fibonacci_filter: bool
    fib_lookback_bars: int
    fib_tolerance_pips: float
    news_blackout_file: str
    news_blackout_before_minutes: int
    news_blackout_after_minutes: int
    trade_journal: str
    deviation_points: int
    magic: int
    comment: str
    dry_run: bool
    live_trading_confirm: bool
    close_on_reverse: bool
    # Backtest realism
    backtest_slippage_pips: float
    backtest_commission_per_lot: float
    mt5_path: Optional[str]
    login: Optional[int]
    password: Optional[str]
    server: Optional[str]

    @classmethod
    def from_file(cls, path: Path) -> "BotConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        login = env_int("MT5_LOGIN") or optional_int(raw.get("login"))
        symbols = raw.get("symbols") or [raw["symbol"]]
        return cls(
            symbol=str(raw["symbol"]),
            symbols=[str(s) for s in symbols],
            timeframe=str(raw.get("timeframe", "M5")).upper(),
            bars=int(raw.get("bars", 250)),
            poll_seconds=int(raw.get("poll_seconds", 30)),
            max_bar_age_minutes=int(raw.get("max_bar_age_minutes", 30)),
            fast_ema=int(raw.get("fast_ema", 20)),
            slow_ema=int(raw.get("slow_ema", 50)),
            trend_timeframe=str(raw.get("trend_timeframe", "H1")).upper(),
            trend_ema=int(raw.get("trend_ema", 200)),
            rsi_period=int(raw.get("rsi_period", 14)),
            buy_rsi_max=float(raw.get("buy_rsi_max", 68)),
            sell_rsi_min=float(raw.get("sell_rsi_min", 32)),
            atr_period=int(raw.get("atr_period", 14)),
            use_atr_stops=bool(raw.get("use_atr_stops", True)),
            atr_sl_multiplier=float(raw.get("atr_sl_multiplier", 1.8)),
            atr_tp_multiplier=float(raw.get("atr_tp_multiplier", 2.8)),
            min_atr_pips=float(raw.get("min_atr_pips", 3.0)),
            use_adx_filter=bool(raw.get("use_adx_filter", True)),
            adx_period=int(raw.get("adx_period", 14)),
            adx_min_threshold=float(raw.get("adx_min_threshold", 20.0)),
            use_macd_confirm=bool(raw.get("use_macd_confirm", True)),
            macd_fast=int(raw.get("macd_fast", 12)),
            macd_slow=int(raw.get("macd_slow", 26)),
            macd_signal_period=int(raw.get("macd_signal_period", 9)),
            risk_percent=float(raw.get("risk_percent", 0.25)),
            fixed_lot=optional_float(raw.get("fixed_lot")),
            stop_loss_pips=float(raw.get("stop_loss_pips", 20)),
            take_profit_pips=float(raw.get("take_profit_pips", 40)),
            max_spread_pips=float(raw.get("max_spread_pips", 2.0)),
            max_positions=int(raw.get("max_positions", 1)),
            max_total_positions=int(raw.get("max_total_positions", 3)),
            max_total_risk_percent=float(raw.get("max_total_risk_percent", 3.0)),
            max_margin_percent=float(raw.get("max_margin_percent", 30.0)),
            daily_loss_limit_percent=float(raw.get("daily_loss_limit_percent", 3.0)),
            weekly_loss_limit_percent=float(raw.get("weekly_loss_limit_percent", 8.0)),
            max_consecutive_losses=int(raw.get("max_consecutive_losses", 3)),
            trailing_stop_pips=float(raw.get("trailing_stop_pips", 15)),
            breakeven_after_pips=float(raw.get("breakeven_after_pips", 12)),
            partial_tp_enabled=bool(raw.get("partial_tp_enabled", True)),
            partial_tp_percent=float(raw.get("partial_tp_percent", 50.0)),
            partial_tp_multiplier=float(raw.get("partial_tp_multiplier", 1.5)),
            trade_start_hour=int(raw.get("trade_start_hour", 0)),
            trade_end_hour=int(raw.get("trade_end_hour", 24)),
            session_preset=str(raw.get("session_preset", "custom")).lower(),
            use_support_resistance=bool(raw.get("use_support_resistance", True)),
            sr_lookback_bars=int(raw.get("sr_lookback_bars", 80)),
            sr_min_distance_pips=float(raw.get("sr_min_distance_pips", 8.0)),
            use_fibonacci_filter=bool(raw.get("use_fibonacci_filter", True)),
            fib_lookback_bars=int(raw.get("fib_lookback_bars", 120)),
            fib_tolerance_pips=float(raw.get("fib_tolerance_pips", 12.0)),
            news_blackout_file=str(raw.get("news_blackout_file", "news_blackouts.json")),
            news_blackout_before_minutes=int(raw.get("news_blackout_before_minutes", 30)),
            news_blackout_after_minutes=int(raw.get("news_blackout_after_minutes", 30)),
            trade_journal=str(raw.get("trade_journal", "trade_journal.csv")),
            deviation_points=int(raw.get("deviation_points", 20)),
            magic=int(raw.get("magic", 230504)),
            comment=str(raw.get("comment", "mt5_python_bot")),
            dry_run=bool(raw.get("dry_run", True)),
            live_trading_confirm=bool(raw.get("live_trading_confirm", False)),
            close_on_reverse=bool(raw.get("close_on_reverse", True)),
            backtest_slippage_pips=float(raw.get("backtest_slippage_pips", 0.5)),
            backtest_commission_per_lot=float(raw.get("backtest_commission_per_lot", 7.0)),
            mt5_path=os.getenv("MT5_PATH") or optional_str(raw.get("mt5_path")),
            login=login,
            password=os.getenv("MT5_PASSWORD") or optional_str(raw.get("password")),
            server=os.getenv("MT5_SERVER") or optional_str(raw.get("server")),
        )

    def validate(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {self.timeframe}")
        if self.trend_timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported trend_timeframe: {self.trend_timeframe}")
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema must be lower than slow_ema")
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be lower than macd_slow")
        min_bars = max(self.slow_ema, self.rsi_period, self.macd_slow + self.macd_signal_period,
                       self.adx_period * 3) + 5
        if self.bars < min_bars:
            raise ValueError(f"bars={self.bars} is too low for configured indicators (need >={min_bars})")
        if self.risk_percent <= 0 and self.fixed_lot is None:
            raise ValueError("Set a positive risk_percent or fixed_lot")
        if self.stop_loss_pips <= 0 or self.take_profit_pips <= 0:
            raise ValueError("stop_loss_pips and take_profit_pips must be positive")
        if not 0 <= self.trade_start_hour <= 23 or not 1 <= self.trade_end_hour <= 24:
            raise ValueError("trade_start_hour must be 0-23 and trade_end_hour must be 1-24")
        if not self.symbols:
            raise ValueError("At least one symbol must be configured")
        if not self.dry_run and not self.live_trading_confirm:
            raise ValueError("Live trading blocked: set live_trading_confirm=true as well as dry_run=false")
        if not 0 < self.partial_tp_percent < 100:
            raise ValueError("partial_tp_percent must be between 0 and 100")


@dataclass(frozen=True)
class MarketSignal:
    side: str
    reason: str

    @property
    def is_trade(self) -> bool:
        return self.side in {"BUY", "SELL"}


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    bars: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_return: float
    max_drawdown: float
    profit_factor: float
    sharpe_ratio: float
    score: float


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def env_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    return int(value) if value else None


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("mt5_bot.log", encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# MT5 connection & symbol helpers
# ---------------------------------------------------------------------------

def initialize_mt5(config: BotConfig) -> None:
    kwargs: dict[str, Any] = {}
    if config.mt5_path:
        kwargs["path"] = config.mt5_path
    if config.login:
        kwargs["login"] = config.login
    if config.password:
        kwargs["password"] = config.password
    if config.server:
        kwargs["server"] = config.server

    if not mt5.initialize(**kwargs):
        code, message = mt5.last_error()
        hint = (
            " Open MT5 and log in, or set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, "
            "and optionally MT5_PATH before running the bot."
        )
        raise RuntimeError(f"MT5 initialize failed: {code} {message}.{hint}")

    account = mt5.account_info()
    if account is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"MT5 account_info failed: {code} {message}")

    logging.info(
        "Connected to MT5 account=%s server=%s balance=%.2f equity=%.2f currency=%s",
        account.login, account.server, account.balance, account.equity, account.currency,
    )


def find_symbol(symbol: str) -> Optional[str]:
    if mt5.symbol_info(symbol) is not None:
        return symbol
    target = symbol.upper()
    symbols = mt5.symbols_get(f"*{symbol}*") or []
    for candidate in symbols:
        upper = candidate.name.upper()
        if upper == target or upper.startswith(target):
            return candidate.name
    return symbols[0].name if symbols else None


def ensure_symbol(symbol: str) -> Any:
    resolved = find_symbol(symbol)
    if resolved is None:
        matches = mt5.symbols_get(f"*{symbol[:3]}*") or []
        examples = ", ".join(item.name for item in matches[:10]) or "no similar symbols found"
        raise RuntimeError(
            f"Symbol not available in MT5: {symbol}. Examples: {examples}"
        )
    if resolved != symbol:
        logging.info("Using broker symbol %s for configured symbol %s", resolved, symbol)
        symbol = resolved
    info = mt5.symbol_info(symbol)
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select symbol in Market Watch: {symbol}")
    return mt5.symbol_info(symbol)


def rate_to_dict(row: Any) -> dict[str, Any]:
    names = getattr(getattr(row, "dtype", None), "names", None)
    if names:
        return {name: row[name].item() if hasattr(row[name], "item") else row[name] for name in names}
    return dict(row)


def resolve_config_symbols(config: BotConfig) -> None:
    resolved_symbols: list[str] = []
    for requested in config.symbols:
        resolved = find_symbol(requested)
        if resolved is None:
            matches = mt5.symbols_get(f"*{requested[:3]}*") or []
            examples = ", ".join(item.name for item in matches[:10]) or "none"
            logging.warning("Skipping unavailable symbol %s. Examples: %s", requested, examples)
            continue
        if resolved != requested:
            logging.info("Resolved %s -> %s", requested, resolved)
        ensure_symbol(resolved)
        resolved_symbols.append(resolved)
    if not resolved_symbols:
        raise RuntimeError("None of the configured symbols are available in MT5")
    config.symbols = resolved_symbols
    config.symbol = resolved_symbols[0]


def get_rates(config: BotConfig) -> list[dict[str, Any]]:
    rates = mt5.copy_rates_from_pos(config.symbol, TIMEFRAMES[config.timeframe], 0, config.bars)
    if rates is None or len(rates) == 0:
        code, message = mt5.last_error()
        raise RuntimeError(f"Could not load rates: {code} {message}")
    return [rate_to_dict(row) for row in rates]


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * alpha + result[-1] * (1 - alpha))
    return result


def rsi(values: list[float], period: int) -> list[float]:
    if len(values) <= period:
        return []
    gains: list[float] = []
    losses: list[float] = []
    for prev, curr in zip(values, values[1:]):
        delta = curr - prev
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    readings: list[float] = [100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)]
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        readings.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return readings


def atr(rates: list[dict[str, Any]], period: int) -> list[float]:
    if len(rates) <= period:
        return []
    true_ranges: list[float] = []
    prev_close = float(rates[0]["close"])
    for row in rates[1:]:
        h, l = float(row["high"]), float(row["low"])
        true_ranges.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = float(row["close"])
    first = sum(true_ranges[:period]) / period
    readings = [first]
    for v in true_ranges[period:]:
        readings.append((readings[-1] * (period - 1) + v) / period)
    return readings


def adx(rates: list[dict[str, Any]], period: int) -> list[float]:
    """Average Directional Index using Wilder smoothing."""
    if len(rates) < period * 3:
        return []

    plus_dms: list[float] = []
    minus_dms: list[float] = []
    true_ranges: list[float] = []
    prev = rates[0]
    for row in rates[1:]:
        h, l = float(row["high"]), float(row["low"])
        ph, pl = float(prev["high"]), float(prev["low"])
        pc = float(prev["close"])
        up = h - ph
        dn = pl - l
        plus_dms.append(up if up > dn and up > 0 else 0.0)
        minus_dms.append(dn if dn > up and dn > 0 else 0.0)
        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
        prev = row

    def _wilder(vals: list[float], p: int) -> list[float]:
        if len(vals) < p:
            return []
        out = [sum(vals[:p])]
        for v in vals[p:]:
            out.append(out[-1] - out[-1] / p + v)
        return out

    str_ = _wilder(true_ranges, period)
    spdm = _wilder(plus_dms, period)
    sndm = _wilder(minus_dms, period)

    dx_values: list[float] = []
    for tr, pdm, ndm in zip(str_, spdm, sndm):
        if tr == 0:
            dx_values.append(0.0)
            continue
        pdi = 100 * pdm / tr
        ndi = 100 * ndm / tr
        denom = pdi + ndi
        dx_values.append(100 * abs(pdi - ndi) / denom if denom > 0 else 0.0)

    return _wilder(dx_values, period)


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9
) -> tuple[list[float], list[float], list[float]]:
    """Returns (macd_line, signal_line, histogram) all the same length."""
    fast_vals = ema(closes, fast)
    slow_vals = ema(closes, slow)
    if not fast_vals or not slow_vals:
        return [], [], []
    macd_line = [f - s for f, s in zip(fast_vals[-len(slow_vals):], slow_vals)]
    if not macd_line:
        return [], [], []
    sig = ema(macd_line, signal_period)
    if not sig:
        return [], [], []
    macd_aligned = macd_line[-len(sig):]
    histogram = [m - s for m, s in zip(macd_aligned, sig)]
    return macd_aligned, sig, histogram


# ---------------------------------------------------------------------------
# Session & filters
# ---------------------------------------------------------------------------

def trend_bias(config: BotConfig) -> str:
    rates = mt5.copy_rates_from_pos(
        config.symbol, TIMEFRAMES[config.trend_timeframe], 0, config.trend_ema + 20
    )
    if rates is None or len(rates) < config.trend_ema + 2:
        return "NEUTRAL"
    closes = [float(r["close"]) for r in rates]
    trend = ema(closes, config.trend_ema)
    if not trend:
        return "NEUTRAL"
    return "BULL" if closes[-1] > trend[-1] else "BEAR"


def in_trading_session(config: BotConfig) -> bool:
    presets = {
        "all": (0, 24),
        "london": (8, 17),
        "new_york": (13, 22),
        "london_new_york_overlap": (13, 17),
    }
    if config.session_preset in presets:
        start_hour, end_hour = presets[config.session_preset]
    else:
        start_hour, end_hour = config.trade_start_hour, config.trade_end_hour
    now = datetime.now().time()
    start = dt_time(start_hour, 0)
    end = dt_time(23, 59, 59) if end_hour == 24 else dt_time(end_hour, 0)
    return (start <= now <= end) if start <= end else (now >= start or now <= end)


def near_news_blackout(config: BotConfig) -> bool:
    path = Path(config.news_blackout_file)
    if not path.exists():
        return False
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("Could not parse news blackout file: %s", path)
        return False
    now = datetime.now(timezone.utc)
    currencies = {config.symbol[:3].upper(), config.symbol[3:6].upper()}
    for event in events:
        if str(event.get("impact", "")).lower() not in {"high", "red"}:
            continue
        if str(event.get("currency", "")).upper() not in currencies:
            continue
        event_time = datetime.fromisoformat(str(event["time_utc"]).replace("Z", "+00:00"))
        delta = (now - event_time).total_seconds()
        if -(config.news_blackout_before_minutes * 60) <= delta <= config.news_blackout_after_minutes * 60:
            logging.info("News blackout active for %s at %s", event.get("currency"), event_time.isoformat())
            return True
    return False


def support_resistance_allows(
    config: BotConfig, rates: list[dict[str, Any]], side: str, symbol_info: Any
) -> tuple[bool, str]:
    if not config.use_support_resistance:
        return True, "S/R disabled"
    lookback = rates[-config.sr_lookback_bars:]
    if len(lookback) < 10:
        return True, "not enough S/R data"
    price = float(rates[-1]["close"])
    resistance = max(float(r["high"]) for r in lookback[:-1])
    support = min(float(r["low"]) for r in lookback[:-1])
    pip = pip_size(symbol_info)
    distance = (resistance - price) / pip if side == "BUY" else (price - support) / pip
    if distance < config.sr_min_distance_pips:
        level = resistance if side == "BUY" else support
        return False, f"too close to {'resistance' if side == 'BUY' else 'support'} {level:.5f}"
    return True, f"S/R distance {distance:.1f} pips"


def fibonacci_allows(
    config: BotConfig, rates: list[dict[str, Any]], side: str, symbol_info: Any
) -> tuple[bool, str]:
    if not config.use_fibonacci_filter:
        return True, "fib disabled"
    lookback = rates[-config.fib_lookback_bars:]
    if len(lookback) < 20:
        return True, "not enough fib data"
    swing_high = max(float(r["high"]) for r in lookback)
    swing_low = min(float(r["low"]) for r in lookback)
    span = swing_high - swing_low
    if span <= 0:
        return True, "flat fib range"
    price = float(rates[-1]["close"])
    pip = pip_size(symbol_info)
    if side == "BUY":
        levels = [swing_high - span * ratio for ratio in (0.382, 0.5, 0.618)]
    else:
        levels = [swing_low + span * ratio for ratio in (0.382, 0.5, 0.618)]
    nearest = min(abs(price - level) / pip for level in levels)
    if nearest > config.fib_tolerance_pips:
        return False, f"not near fib, nearest={nearest:.1f} pips"
    return True, f"near fib, nearest={nearest:.1f} pips"


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def build_signal(config: BotConfig, rates: list[dict[str, Any]], symbol_info: Any) -> MarketSignal:
    closes = [float(r["close"]) for r in rates]
    fast = ema(closes, config.fast_ema)
    slow = ema(closes, config.slow_ema)
    rsi_vals = rsi(closes, config.rsi_period)
    atr_vals = atr(rates, config.atr_period)

    if len(fast) < 2 or len(slow) < 2 or not rsi_vals or not atr_vals:
        return MarketSignal("HOLD", "not enough indicator data")

    fast_now, fast_prev = fast[-1], fast[-2]
    slow_now, slow_prev = slow[-1], slow[-2]
    rsi_now = rsi_vals[-1]
    atr_pips = atr_vals[-1] / pip_size(symbol_info)
    bias = trend_bias(config)

    if atr_pips < config.min_atr_pips:
        return MarketSignal("HOLD", f"low volatility ATR={atr_pips:.1f} pips")

    # ADX: only trade trending markets
    if config.use_adx_filter:
        adx_vals = adx(rates, config.adx_period)
        if not adx_vals:
            return MarketSignal("HOLD", "not enough ADX data")
        adx_now = adx_vals[-1]
        if adx_now < config.adx_min_threshold:
            return MarketSignal("HOLD", f"ranging market ADX={adx_now:.1f} < {config.adx_min_threshold}")

    # MACD: confirm signal direction
    macd_bull = True
    macd_bear = True
    macd_tag = ""
    if config.use_macd_confirm:
        _, _, hist = macd(closes, config.macd_fast, config.macd_slow, config.macd_signal_period)
        if len(hist) < 2:
            return MarketSignal("HOLD", "not enough MACD data")
        h = hist[-1]
        macd_bull = h > 0
        macd_bear = h < 0
        macd_tag = f" MACD_h={h:.5f}"

    bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
    bearish_cross = fast_prev >= slow_prev and fast_now < slow_now

    if bullish_cross and rsi_now <= config.buy_rsi_max and bias == "BULL":
        if not macd_bull:
            return MarketSignal("HOLD", f"buy filtered by MACD{macd_tag}")
        ok_sr, sr_msg = support_resistance_allows(config, rates, "BUY", symbol_info)
        ok_fib, fib_msg = fibonacci_allows(config, rates, "BUY", symbol_info)
        if ok_sr and ok_fib:
            return MarketSignal(
                "BUY",
                f"EMA cross RSI={rsi_now:.1f} bias={bias} ATR={atr_pips:.1f}{macd_tag} {sr_msg} {fib_msg}",
            )
        return MarketSignal("HOLD", f"buy filtered: {sr_msg} {fib_msg}")

    if bearish_cross and rsi_now >= config.sell_rsi_min and bias == "BEAR":
        if not macd_bear:
            return MarketSignal("HOLD", f"sell filtered by MACD{macd_tag}")
        ok_sr, sr_msg = support_resistance_allows(config, rates, "SELL", symbol_info)
        ok_fib, fib_msg = fibonacci_allows(config, rates, "SELL", symbol_info)
        if ok_sr and ok_fib:
            return MarketSignal(
                "SELL",
                f"EMA cross RSI={rsi_now:.1f} bias={bias} ATR={atr_pips:.1f}{macd_tag} {sr_msg} {fib_msg}",
            )
        return MarketSignal("HOLD", f"sell filtered: {sr_msg} {fib_msg}")

    trend = "bullish" if fast_now > slow_now else "bearish"
    return MarketSignal("HOLD", f"no cross trend={trend} RSI={rsi_now:.1f} bias={bias}")


# ---------------------------------------------------------------------------
# Sizing & risk
# ---------------------------------------------------------------------------

def pip_size(symbol_info: Any) -> float:
    return symbol_info.point * 10 if symbol_info.digits in (3, 5) else symbol_info.point


def current_spread_pips(symbol: str, symbol_info: Any) -> float:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"Could not read tick for {symbol}")
    return (tick.ask - tick.bid) / pip_size(symbol_info)


def normalize_volume(symbol_info: Any, volume: float) -> float:
    step = symbol_info.volume_step
    minimum = symbol_info.volume_min
    maximum = symbol_info.volume_max
    stepped = math.floor(volume / step) * step
    bounded = min(max(stepped, minimum), maximum)
    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    return round(bounded, decimals)


def effective_stops(
    config: BotConfig, rates: list[dict[str, Any]], symbol_info: Any
) -> tuple[float, float]:
    if not config.use_atr_stops:
        return config.stop_loss_pips, config.take_profit_pips
    atr_vals = atr(rates, config.atr_period)
    if not atr_vals:
        return config.stop_loss_pips, config.take_profit_pips
    current_atr_pips = atr_vals[-1] / pip_size(symbol_info)
    sl = max(config.stop_loss_pips, current_atr_pips * config.atr_sl_multiplier)
    tp = max(config.take_profit_pips, current_atr_pips * config.atr_tp_multiplier)
    return sl, tp


def calculate_volume(config: BotConfig, symbol_info: Any, stop_loss_pips: float) -> float:
    if config.fixed_lot is not None:
        return normalize_volume(symbol_info, config.fixed_lot)
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("Could not read account info for risk sizing")
    risk_amount = account.equity * (config.risk_percent / 100)
    price_distance = stop_loss_pips * pip_size(symbol_info)
    if symbol_info.trade_tick_size <= 0 or symbol_info.trade_tick_value <= 0:
        raise RuntimeError("Symbol does not expose tick value/tick size for risk sizing")
    loss_per_lot = (price_distance / symbol_info.trade_tick_size) * symbol_info.trade_tick_value
    if loss_per_lot <= 0:
        raise RuntimeError("Calculated invalid loss per lot")
    return normalize_volume(symbol_info, risk_amount / loss_per_lot)


def open_positions(config: BotConfig) -> list[Any]:
    positions = mt5.positions_get(symbol=config.symbol)
    if positions is None:
        return []
    return [p for p in positions if p.magic == config.magic]


def all_bot_positions(config: BotConfig) -> list[Any]:
    positions = mt5.positions_get()
    if positions is None:
        return []
    configured = set(config.symbols)
    return [p for p in positions if p.magic == config.magic and p.symbol in configured]


def estimate_position_risk_percent(config: BotConfig, position: Any) -> float:
    account = mt5.account_info()
    symbol_info = mt5.symbol_info(position.symbol)
    if account is None or symbol_info is None or account.equity <= 0 or not position.sl:
        return 0.0
    distance = abs(float(position.price_open) - float(position.sl))
    if symbol_info.trade_tick_size <= 0 or symbol_info.trade_tick_value <= 0:
        return 0.0
    risk = (distance / symbol_info.trade_tick_size) * symbol_info.trade_tick_value * float(position.volume)
    return (risk / account.equity) * 100


def total_open_risk_percent(config: BotConfig) -> float:
    return sum(estimate_position_risk_percent(config, p) for p in all_bot_positions(config))


def margin_allows(config: BotConfig, request: dict[str, Any]) -> bool:
    if config.max_margin_percent <= 0:
        return True
    account = mt5.account_info()
    if account is None or account.equity <= 0:
        return False
    margin = mt5.order_calc_margin(request["type"], request["symbol"], request["volume"], request["price"])
    if margin is None:
        logging.warning("Could not calculate margin for %s", request["symbol"])
        return False
    projected = ((float(account.margin) + float(margin)) / account.equity) * 100
    if projected > config.max_margin_percent:
        logging.info("Skipping: projected margin %.1f%% exceeds max %.1f%%", projected, config.max_margin_percent)
        return False
    return True


# ---------------------------------------------------------------------------
# Loss guards
# ---------------------------------------------------------------------------

def _get_bot_deals_since(config: BotConfig, since: datetime) -> list[Any]:
    deals = mt5.history_deals_get(since, datetime.now())
    if not deals:
        return []
    configured = set(config.symbols)
    return [
        d for d in deals
        if getattr(d, "magic", None) == config.magic
        and getattr(d, "symbol", None) in configured
    ]


def daily_loss_limit_hit(config: BotConfig) -> bool:
    if config.daily_loss_limit_percent <= 0:
        return False
    account = mt5.account_info()
    if account is None or account.balance <= 0:
        return False
    since = datetime.combine(datetime.now().date(), dt_time.min)
    profit = sum(float(d.profit) for d in _get_bot_deals_since(config, since))
    max_loss = account.balance * (config.daily_loss_limit_percent / 100)
    if profit <= -max_loss:
        logging.warning("Daily loss limit hit: P/L=%.2f limit=-%.2f", profit, max_loss)
        return True
    return False


def weekly_loss_limit_hit(config: BotConfig) -> bool:
    if config.weekly_loss_limit_percent <= 0:
        return False
    account = mt5.account_info()
    if account is None or account.balance <= 0:
        return False
    now = datetime.now()
    week_start = now - timedelta(days=now.weekday())
    since = datetime.combine(week_start.date(), dt_time.min)
    profit = sum(float(d.profit) for d in _get_bot_deals_since(config, since))
    max_loss = account.balance * (config.weekly_loss_limit_percent / 100)
    if profit <= -max_loss:
        logging.warning("Weekly loss limit hit: P/L=%.2f limit=-%.2f", profit, max_loss)
        return True
    return False


def consecutive_losses_count(config: BotConfig) -> int:
    since = datetime.now() - timedelta(days=14)
    deals = _get_bot_deals_since(config, since)
    closing = [d for d in deals if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]
    closing.sort(key=lambda d: d.time, reverse=True)
    count = 0
    for deal in closing:
        if float(deal.profit) < 0:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def journal_event(
    config: BotConfig, action: str, side: str, volume: float, price: float,
    reason: str, ticket: Any = "", pnl: float = 0.0
) -> None:
    path = Path(config.trade_journal)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["time_utc", "symbol", "action", "side", "volume", "price", "ticket", "reason", "pnl", "dry_run"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            config.symbol, action, side, volume, price, ticket, reason,
            f"{pnl:.2f}", config.dry_run,
        ])


# ---------------------------------------------------------------------------
# Position management (trailing, breakeven, partial TP)
# ---------------------------------------------------------------------------

def _send_sl_update(config: BotConfig, position: Any, new_sl: float, symbol_info: Any) -> None:
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": config.symbol,
        "position": position.ticket,
        "sl": round(new_sl, symbol_info.digits),
        "tp": position.tp,
        "magic": config.magic,
        "comment": f"{config.comment}_protect",
    }
    if config.dry_run:
        logging.info("DRY RUN SL update: ticket=%s sl=%.5f", position.ticket, new_sl)
        return
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logging.error("SL update rejected: result=%s", result)
    else:
        logging.info("Updated SL: ticket=%s sl=%.5f", position.ticket, new_sl)


def supported_filling_modes(symbol_info: Any) -> list[int]:
    raw_mode = int(getattr(symbol_info, "filling_mode", 0) or 0)
    modes: list[int] = []
    if raw_mode & 1:
        modes.append(mt5.ORDER_FILLING_FOK)
    if raw_mode & 2:
        modes.append(mt5.ORDER_FILLING_IOC)
    if hasattr(mt5, "ORDER_FILLING_RETURN"):
        modes.append(mt5.ORDER_FILLING_RETURN)
    fallback = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK]
    if hasattr(mt5, "ORDER_FILLING_RETURN"):
        fallback.append(mt5.ORDER_FILLING_RETURN)
    for mode in fallback:
        if mode not in modes:
            modes.append(mode)
    return modes


def send_deal_with_filling_retry(request: dict[str, Any], symbol_info: Any) -> Any:
    last_result = None
    for filling_mode in supported_filling_modes(symbol_info):
        attempt = dict(request)
        attempt["type_filling"] = filling_mode
        result = mt5.order_send(attempt)
        if result is None:
            code, message = mt5.last_error()
            logging.error("order_send failed filling=%s: %s %s", filling_mode, code, message)
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        last_result = result
        if result.retcode != 10030:
            return result
        logging.info("Retrying %s with alternate filling mode (retcode=10030)", request["symbol"])
    return last_result


def _execute_partial_close(
    config: BotConfig, position: Any, volume: float, tick: Any, symbol_info: Any
) -> None:
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    price = tick.bid if is_buy else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": config.symbol,
        "position": position.ticket,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "price": price,
        "deviation": config.deviation_points,
        "magic": config.magic,
        "comment": f"{config.comment}_partial_tp",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    if config.dry_run:
        logging.info("DRY RUN partial TP: ticket=%s vol=%.2f price=%.5f", position.ticket, volume, price)
        journal_event(config, "DRY_PARTIAL_TP", "SELL" if is_buy else "BUY", volume, price, "partial_tp", position.ticket)
        return
    result = send_deal_with_filling_retry(request, symbol_info)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logging.error("Partial TP rejected: result=%s", result)
    else:
        logging.info("Partial TP taken: ticket=%s vol=%.2f", position.ticket, volume)
        journal_event(config, "PARTIAL_TP", "SELL" if is_buy else "BUY", volume, price, "partial_tp", position.ticket)


def manage_open_positions(config: BotConfig, symbol_info: Any) -> None:
    tick = mt5.symbol_info_tick(config.symbol)
    if tick is None:
        return

    pip = pip_size(symbol_info)
    atr_cache: Optional[float] = None

    for position in open_positions(config):
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        current = tick.bid if is_buy else tick.ask
        entry = float(position.price_open)
        profit_pips = (current - entry) / pip if is_buy else (entry - current) / pip
        new_sl = float(position.sl or 0.0)
        ticket = position.ticket

        # Partial take-profit at partial_tp_multiplier * ATR
        if config.partial_tp_enabled and ticket not in _partial_tp_taken:
            if atr_cache is None:
                try:
                    raw = mt5.copy_rates_from_pos(config.symbol, TIMEFRAMES[config.timeframe], 0, config.bars)
                    rates_tmp = [rate_to_dict(r) for r in raw] if raw is not None else []
                    atr_vals = atr(rates_tmp, config.atr_period)
                    atr_cache = (atr_vals[-1] / pip) if atr_vals else 0.0
                except Exception:
                    atr_cache = 0.0
            if atr_cache > 0:
                partial_threshold = atr_cache * config.partial_tp_multiplier
                if profit_pips >= partial_threshold:
                    partial_vol = normalize_volume(symbol_info, position.volume * config.partial_tp_percent / 100)
                    if partial_vol >= symbol_info.volume_min:
                        _execute_partial_close(config, position, partial_vol, tick, symbol_info)
                        _partial_tp_taken.add(ticket)
                        # Move SL to breakeven after partial close
                        be_sl = entry + pip if is_buy else entry - pip
                        new_sl = max(new_sl, be_sl) if is_buy else min(new_sl or be_sl, be_sl)

        # Breakeven protection
        if config.breakeven_after_pips > 0 and profit_pips >= config.breakeven_after_pips:
            be = entry + pip if is_buy else entry - pip
            new_sl = max(new_sl, be) if is_buy else min(new_sl or be, be)

        # Trailing stop
        if config.trailing_stop_pips > 0 and profit_pips >= config.trailing_stop_pips:
            trailed = current - config.trailing_stop_pips * pip if is_buy else current + config.trailing_stop_pips * pip
            new_sl = max(new_sl, trailed) if is_buy else min(new_sl or trailed, trailed)

        if new_sl and abs(new_sl - float(position.sl or 0.0)) >= symbol_info.point:
            _send_sl_update(config, position, new_sl, symbol_info)


# ---------------------------------------------------------------------------
# Order placement & closing
# ---------------------------------------------------------------------------

def make_order_request(
    config: BotConfig, side: str, volume: float,
    symbol_info: Any, stop_loss_pips: float, take_profit_pips: float,
) -> dict[str, Any]:
    tick = mt5.symbol_info_tick(config.symbol)
    if tick is None:
        raise RuntimeError(f"Could not read tick for {config.symbol}")
    pip = pip_size(symbol_info)
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if side == "BUY" else tick.bid
    sl = price - stop_loss_pips * pip if side == "BUY" else price + stop_loss_pips * pip
    tp = price + take_profit_pips * pip if side == "BUY" else price - take_profit_pips * pip
    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": config.symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": round(sl, symbol_info.digits),
        "tp": round(tp, symbol_info.digits),
        "deviation": config.deviation_points,
        "magic": config.magic,
        "comment": config.comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }


def place_order(
    config: BotConfig, signal_: MarketSignal, symbol_info: Any, rates: list[dict[str, Any]]
) -> None:
    spread = current_spread_pips(config.symbol, symbol_info)
    if spread > config.max_spread_pips:
        logging.info("Skipping %s: spread %.2f exceeds max %.2f", signal_.side, spread, config.max_spread_pips)
        return

    sl_pips, tp_pips = effective_stops(config, rates, symbol_info)
    projected_risk = total_open_risk_percent(config) + config.risk_percent
    if projected_risk > config.max_total_risk_percent:
        logging.info("Skipping %s: projected risk %.2f%% > max %.2f%%", signal_.side, projected_risk, config.max_total_risk_percent)
        return

    volume = calculate_volume(config, symbol_info, sl_pips)
    request = make_order_request(config, signal_.side, volume, symbol_info, sl_pips, tp_pips)
    if not margin_allows(config, request):
        return

    if config.dry_run:
        logging.info("DRY RUN order: %s reason=%s", request, signal_.reason)
        journal_event(config, "DRY_ORDER", signal_.side, volume, request["price"], signal_.reason)
        return

    result = send_deal_with_filling_retry(request, symbol_info)
    if result is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"order_send failed: {code} {message}")
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logging.error("Order rejected: retcode=%s comment=%s", result.retcode, result.comment)
        journal_event(config, "REJECTED", signal_.side, volume, request["price"], result.comment)
        return
    logging.info("Order placed: ticket=%s side=%s volume=%s", result.order, signal_.side, volume)
    journal_event(config, "ORDER", signal_.side, volume, request["price"], signal_.reason, result.order)


def close_position(config: BotConfig, position: Any) -> None:
    tick = mt5.symbol_info_tick(config.symbol)
    if tick is None:
        raise RuntimeError(f"Could not read tick for {config.symbol}")
    symbol_info = mt5.symbol_info(config.symbol)
    if symbol_info is None:
        raise RuntimeError(f"Could not read symbol info for {config.symbol}")

    is_buy = position.type == mt5.POSITION_TYPE_BUY
    price = tick.bid if is_buy else tick.ask
    pnl = float(position.profit)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": config.symbol,
        "position": position.ticket,
        "volume": position.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "price": price,
        "deviation": config.deviation_points,
        "magic": config.magic,
        "comment": f"{config.comment}_close",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    if config.dry_run:
        logging.info("DRY RUN close: ticket=%s pnl=%.2f", position.ticket, pnl)
        journal_event(config, "DRY_CLOSE", "SELL" if is_buy else "BUY", position.volume, price, "close_on_reverse", position.ticket, pnl)
        return
    result = send_deal_with_filling_retry(request, symbol_info)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logging.error("Close rejected: result=%s", result)
        return
    logging.info("Closed position: ticket=%s pnl=%.2f", position.ticket, pnl)
    journal_event(config, "CLOSE", "SELL" if is_buy else "BUY", position.volume, price, "close_on_reverse", position.ticket, pnl)


# ---------------------------------------------------------------------------
# Main trading loop
# ---------------------------------------------------------------------------

def run_once(config: BotConfig) -> None:
    symbol_info = ensure_symbol(config.symbol)
    manage_open_positions(config, symbol_info)

    if not in_trading_session(config):
        logging.info("Outside trading session; entries paused")
        return
    if daily_loss_limit_hit(config):
        logging.info("Daily loss guard active; entries paused")
        return
    if weekly_loss_limit_hit(config):
        logging.info("Weekly loss guard active; entries paused")
        return
    if config.max_consecutive_losses > 0:
        consec = consecutive_losses_count(config)
        if consec >= config.max_consecutive_losses:
            logging.warning("Consecutive loss cooldown: %d losses (max=%d); entries paused", consec, config.max_consecutive_losses)
            return
    if near_news_blackout(config):
        logging.info("News guard active for %s; entries paused", config.symbol)
        return
    if len(all_bot_positions(config)) >= config.max_total_positions:
        logging.info("Max total positions reached; entries paused")
        return

    positions = open_positions(config)
    rates = get_rates(config)
    latest_bar = datetime.fromtimestamp(rates[-1]["time"], timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - latest_bar).total_seconds() / 60
    if age_minutes > config.max_bar_age_minutes:
        logging.warning("Stale bar for %s: age=%.1f min", config.symbol, age_minutes)
        return

    signal_ = build_signal(config, rates, symbol_info)
    logging.info(
        "%s signal=%s reason=%s positions=%d",
        config.symbol, signal_.side, signal_.reason, len(positions),
    )

    if not signal_.is_trade:
        return

    opposite_type = mt5.POSITION_TYPE_SELL if signal_.side == "BUY" else mt5.POSITION_TYPE_BUY
    if len(positions) >= config.max_positions:
        if config.close_on_reverse:
            for p in positions:
                if p.type == opposite_type:
                    close_position(config, p)
        logging.info("Max symbol positions reached; no new entry")
        return

    place_order(config, signal_, symbol_info, rates)


def run_all_symbols_once(config: BotConfig) -> None:
    for symbol in config.symbols:
        config.symbol = symbol
        try:
            run_once(config)
        except Exception:
            logging.exception("Cycle failed for %s", symbol)


def run_loop(config: BotConfig) -> None:
    stop = False

    def request_stop(signum: int, _: Any) -> None:
        nonlocal stop
        logging.info("Signal %s received; stopping after current cycle", signum)
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stop:
        run_all_symbols_once(config)
        time.sleep(config.poll_seconds)


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------

def backtest_signal(config: BotConfig, rates: list[dict[str, Any]], symbol_info: Any) -> MarketSignal:
    closes = [float(r["close"]) for r in rates]
    fast = ema(closes, config.fast_ema)
    slow = ema(closes, config.slow_ema)
    rsi_vals = rsi(closes, config.rsi_period)
    atr_vals = atr(rates, config.atr_period)

    if len(fast) < 2 or len(slow) < 3 or not rsi_vals or not atr_vals:
        return MarketSignal("HOLD", "not enough data")

    if atr_vals[-1] / pip_size(symbol_info) < config.min_atr_pips:
        return MarketSignal("HOLD", "low ATR")

    if config.use_adx_filter:
        adx_vals = adx(rates, config.adx_period)
        if not adx_vals:
            return MarketSignal("HOLD", "no ADX data")
        if adx_vals[-1] < config.adx_min_threshold:
            return MarketSignal("HOLD", "low ADX")

    hist_now: Optional[float] = None
    if config.use_macd_confirm:
        _, _, hist = macd(closes, config.macd_fast, config.macd_slow, config.macd_signal_period)
        if len(hist) < 1:
            return MarketSignal("HOLD", "no MACD data")
        hist_now = hist[-1]

    bullish_ctx = slow[-1] > slow[-3]
    bearish_ctx = slow[-1] < slow[-3]

    if fast[-2] <= slow[-2] and fast[-1] > slow[-1] and rsi_vals[-1] <= config.buy_rsi_max and bullish_ctx:
        if hist_now is not None and hist_now <= 0:
            return MarketSignal("HOLD", "buy blocked by MACD")
        return MarketSignal("BUY", "backtest buy")

    if fast[-2] >= slow[-2] and fast[-1] < slow[-1] and rsi_vals[-1] >= config.sell_rsi_min and bearish_ctx:
        if hist_now is not None and hist_now >= 0:
            return MarketSignal("HOLD", "sell blocked by MACD")
        return MarketSignal("SELL", "backtest sell")

    return MarketSignal("HOLD", "no setup")


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    std = (sum((r - mean) ** 2 for r in returns) / (n - 1)) ** 0.5
    return (mean / std) * (252 ** 0.5) if std > 0 else 0.0


def run_backtest(config: BotConfig, bars: int) -> BacktestResult:
    symbol_info = ensure_symbol(config.symbol)
    rates_raw = mt5.copy_rates_from_pos(config.symbol, TIMEFRAMES[config.timeframe], 0, bars)
    if rates_raw is None or len(rates_raw) < config.bars:
        raise RuntimeError("Not enough MT5 history for backtest")
    rates = [rate_to_dict(r) for r in rates_raw]
    pip = pip_size(symbol_info)
    balance = 10_000.0
    start_balance = balance
    wins = losses = trades = 0
    equity_high = balance
    max_drawdown = 0.0
    gross_profit = gross_loss = 0.0
    trade_returns: list[float] = []
    position: Optional[dict[str, Any]] = None

    slippage = config.backtest_slippage_pips * pip
    commission_rt = 2 * config.backtest_commission_per_lot * symbol_info.volume_min

    warmup = max(
        config.bars, config.trend_ema,
        config.slow_ema + config.rsi_period + config.atr_period,
        config.adx_period * 3,
        config.macd_slow + config.macd_signal_period,
    )

    for index in range(warmup, len(rates)):
        row = rates[index]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        if position:
            side = position["side"]
            hit_sl = (low <= position["sl"]) if side == "BUY" else (high >= position["sl"])
            hit_tp = (high >= position["tp"]) if side == "BUY" else (low <= position["tp"])
            if hit_sl or hit_tp:
                exit_price = position["sl"] if hit_sl else position["tp"]
                # Adverse slippage on SL exits
                if hit_sl:
                    exit_price = exit_price - slippage if side == "BUY" else exit_price + slippage
                pnl_pips = (exit_price - position["entry"]) / pip if side == "BUY" else (position["entry"] - exit_price) / pip
                risk_amount = balance * (config.risk_percent / 100)
                pnl = risk_amount * (pnl_pips / position["risk_pips"]) - commission_rt
                balance += pnl
                if pnl > 0:
                    wins += 1
                    gross_profit += pnl
                else:
                    losses += 1
                    gross_loss += abs(pnl)
                trades += 1
                trade_returns.append(pnl / max(balance, 1) * 100)
                position = None
                equity_high = max(equity_high, balance)
                max_drawdown = max(max_drawdown, (equity_high - balance) / equity_high)
                continue

        if position:
            continue

        window = rates[index - config.bars: index]
        signal_ = backtest_signal(config, window, symbol_info)
        if not signal_.is_trade:
            continue

        sl_pips, tp_pips = effective_stops(config, window, symbol_info)
        entry = close + slippage if signal_.side == "BUY" else close - slippage
        if signal_.side == "BUY":
            position = {"side": "BUY", "entry": entry, "sl": entry - sl_pips * pip, "tp": entry + tp_pips * pip, "risk_pips": sl_pips}
        else:
            position = {"side": "SELL", "entry": entry, "sl": entry + sl_pips * pip, "tp": entry - tp_pips * pip, "risk_pips": sl_pips}

    win_rate = (wins / trades * 100) if trades else 0.0
    net_return = (balance - start_balance) / start_balance * 100
    dd_pct = max_drawdown * 100
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    sharpe = _sharpe(trade_returns)
    score = net_return - dd_pct * 1.5 + sharpe * 5

    result = BacktestResult(
        symbol=config.symbol, bars=len(rates), trades=trades, wins=wins, losses=losses,
        win_rate=win_rate, net_return=net_return, max_drawdown=dd_pct,
        profit_factor=pf, sharpe_ratio=sharpe, score=score,
    )
    logging.info(
        "Backtest %s bars=%d trades=%d wr=%.1f%% ret=%.2f%% dd=%.2f%% PF=%.2f sharpe=%.2f",
        result.symbol, result.bars, result.trades, result.win_rate,
        result.net_return, result.max_drawdown, result.profit_factor, result.sharpe_ratio,
    )
    return result


def run_all_symbol_backtests(config: BotConfig, bars: int) -> list[BacktestResult]:
    results: list[BacktestResult] = []
    for symbol in config.symbols:
        config.symbol = symbol
        try:
            results.append(run_backtest(config, bars))
        except Exception as exc:
            logging.warning("Backtest skipped for %s: %s", symbol, exc)
    results.sort(key=lambda r: r.score, reverse=True)
    if results:
        logging.info("Backtest ranking:")
        for i, r in enumerate(results, 1):
            logging.info(
                "%d. %s score=%.2f ret=%.2f%% dd=%.2f%% PF=%.2f sharpe=%.2f wr=%.1f%% trades=%d",
                i, r.symbol, r.score, r.net_return, r.max_drawdown,
                r.profit_factor, r.sharpe_ratio, r.win_rate, r.trades,
            )
    return results


def write_backtest_results(path: Path, results: list[BacktestResult], extra: Optional[dict[str, Any]] = None) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        fieldnames = ["time_utc", "symbol", "bars", "trades", "wins", "losses",
                      "win_rate", "net_return", "max_drawdown", "profit_factor", "sharpe_ratio", "score", "settings"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for r in results:
            writer.writerow({
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "symbol": r.symbol, "bars": r.bars, "trades": r.trades,
                "wins": r.wins, "losses": r.losses,
                "win_rate": f"{r.win_rate:.2f}", "net_return": f"{r.net_return:.2f}",
                "max_drawdown": f"{r.max_drawdown:.2f}", "profit_factor": f"{r.profit_factor:.2f}",
                "sharpe_ratio": f"{r.sharpe_ratio:.2f}", "score": f"{r.score:.2f}",
                "settings": json.dumps(extra or {}, sort_keys=True),
            })


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

def run_optimizer(config: BotConfig, bars: int, top: int) -> None:
    original = {
        "fast_ema": config.fast_ema, "slow_ema": config.slow_ema,
        "buy_rsi_max": config.buy_rsi_max, "sell_rsi_min": config.sell_rsi_min,
        "atr_sl_multiplier": config.atr_sl_multiplier, "atr_tp_multiplier": config.atr_tp_multiplier,
        "adx_min_threshold": config.adx_min_threshold,
    }
    candidates: list[tuple[BacktestResult, dict[str, Any]]] = []

    for fast in [10, 20]:
        for slow in [30, 50]:
            if fast >= slow:
                continue
            for buy_rsi in [60, 68]:
                for sell_rsi in [32, 40]:
                    for atr_sl in [1.5, 1.8]:
                        for atr_tp in [2.2, 2.8]:
                            for adx_thresh in [20.0, 25.0]:
                                config.fast_ema = fast
                                config.slow_ema = slow
                                config.buy_rsi_max = buy_rsi
                                config.sell_rsi_min = sell_rsi
                                config.atr_sl_multiplier = atr_sl
                                config.atr_tp_multiplier = atr_tp
                                config.adx_min_threshold = adx_thresh
                                settings = {
                                    "fast_ema": fast, "slow_ema": slow,
                                    "buy_rsi_max": buy_rsi, "sell_rsi_min": sell_rsi,
                                    "atr_sl_multiplier": atr_sl, "atr_tp_multiplier": atr_tp,
                                    "adx_min_threshold": adx_thresh,
                                }
                                for result in run_all_symbol_backtests(config, bars):
                                    candidates.append((result, settings))

    for key, value in original.items():
        setattr(config, key, value)

    candidates.sort(key=lambda x: x[0].score, reverse=True)
    best = candidates[:top]
    logging.info("Optimizer top %d results:", len(best))
    for i, (r, s) in enumerate(best, 1):
        logging.info(
            "%d. %s score=%.2f ret=%.2f%% dd=%.2f%% PF=%.2f sharpe=%.2f trades=%d settings=%s",
            i, r.symbol, r.score, r.net_return, r.max_drawdown,
            r.profit_factor, r.sharpe_ratio, r.trades, s,
        )

    with Path("optimizer_results.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["rank", "symbol", "bars", "trades", "win_rate", "net_return",
                      "max_drawdown", "profit_factor", "sharpe_ratio", "score", "settings"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for i, (r, s) in enumerate(best, 1):
            writer.writerow({
                "rank": i, "symbol": r.symbol, "bars": r.bars, "trades": r.trades,
                "win_rate": f"{r.win_rate:.2f}", "net_return": f"{r.net_return:.2f}",
                "max_drawdown": f"{r.max_drawdown:.2f}", "profit_factor": f"{r.profit_factor:.2f}",
                "sharpe_ratio": f"{r.sharpe_ratio:.2f}", "score": f"{r.score:.2f}",
                "settings": json.dumps(s, sort_keys=True),
            })
    logging.info("Optimizer results written to optimizer_results.csv")


# ---------------------------------------------------------------------------
# Forward report
# ---------------------------------------------------------------------------

def run_forward_report(config: BotConfig, days: int) -> None:
    path = Path(config.trade_journal)
    if not path.exists():
        logging.info("No trade journal found: %s", path)
        return

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    rows: list[dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                ts = datetime.fromisoformat(row["time_utc"]).timestamp()
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                rows.append(row)

    if not rows:
        logging.info("No journal events in the last %d day(s)", days)
        return

    by_symbol: dict[str, int] = {}
    by_action: dict[str, int] = {}
    total_pnl = 0.0
    for row in rows:
        sym = row.get("symbol", "")
        act = row.get("action", "")
        by_symbol[sym] = by_symbol.get(sym, 0) + 1
        by_action[act] = by_action.get(act, 0) + 1
        try:
            total_pnl += float(row.get("pnl") or 0)
        except (ValueError, TypeError):
            pass

    logging.info("Forward report last %d day(s): events=%d total_pnl=%.2f", days, len(rows), total_pnl)
    logging.info("By action: %s", by_action)
    logging.info("By symbol: %s", by_symbol)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="MT5 EMA/RSI/ADX/MACD trading bot")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--check", action="store_true", help="Verify MT5 connection and exit")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--backtest", type=int, metavar="BARS")
    parser.add_argument("--optimize", type=int, metavar="BARS")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--forward-report", type=int, metavar="DAYS")
    args = parser.parse_args()

    setup_logging()
    config = BotConfig.from_file(Path(args.config))
    config.validate()
    initialize_mt5(config)
    resolve_config_symbols(config)

    if args.check:
        logging.info("Connection OK: symbols=%s dry_run=%s", ", ".join(config.symbols), config.dry_run)
        mt5.shutdown()
        return 0

    try:
        if args.forward_report:
            run_forward_report(config, args.forward_report)
        elif args.optimize:
            run_optimizer(config, args.optimize, args.top)
        elif args.backtest:
            results = run_all_symbol_backtests(config, args.backtest)
            write_backtest_results(Path("backtest_results.csv"), results)
            logging.info("Backtest results written to backtest_results.csv")
        elif args.once:
            run_all_symbols_once(config)
        else:
            run_loop(config)
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
