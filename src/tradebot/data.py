"""Historical data: CSV loading, symbol specifications, and a synthetic market for tests."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.broker.base import SymbolSpec
from tradebot.config import TIMEFRAME_MINUTES, BotConfig

REQUIRED = ("time", "open", "high", "low", "close")
BAR_COLUMNS = ["time", "open", "high", "low", "close", "spread"]
OPTIONAL_COLUMNS = ("tick_volume",)
"""Kept when the source has them. Tick volume is the broker's count of price changes in the bar:
spot FX has no central volume, so this is the only volume there is."""


def load_bars(path: str | Path) -> pd.DataFrame:
    """Load OHLC bars from CSV (MT5 export format: time, open, high, low, close[, spread, ...])."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    if "spread" not in df:
        df["spread"] = 0
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad.any():
        raise ValueError(f"{path}: {int(bad.sum())} bars have high/low inconsistent with open/close")
    return df[[*BAR_COLUMNS, *(c for c in OPTIONAL_COLUMNS if c in df.columns)]]


def bars_path(data_dir: Path, symbol: str, timeframe: str) -> Path:
    return data_dir / f"{symbol}_{timeframe}.csv"


def load_dataset(
    data_dir: str | Path, symbols: list[str], timeframe: str
) -> tuple[dict[str, pd.DataFrame], dict[str, SymbolSpec]]:
    data_dir = Path(data_dir)
    data, specs = {}, {}
    for symbol in symbols:
        path = bars_path(data_dir, symbol, timeframe)
        if not path.is_file():
            raise FileNotFoundError(
                f"no data for {symbol} {timeframe}: expected {path} (run `tradebot fetch`)"
            )
        data[symbol] = load_bars(path)
        specs[symbol] = load_spec(data_dir, symbol)
    return data, specs


def trend_timeframes(config: BotConfig) -> dict[str, tuple[str, ...]]:
    """Symbol -> the higher timeframes its trend filter reads, for symbols that use any."""
    out = {}
    for symbol in config.symbols:
        p = config.for_symbol(symbol).strategy
        if not p.use_trend_filter:
            continue
        higher = tuple(
            tf for tf in p.trend_ladder(config.timeframe) if TIMEFRAME_MINUTES[tf] > config.bar_minutes
        )
        if higher:
            out[symbol] = higher
    return out


def load_trend_bars(data_dir: str | Path, config: BotConfig) -> dict[tuple[str, str], pd.DataFrame]:
    """Real higher-timeframe bars saved by `tradebot fetch`, keyed by (symbol, timeframe).

    With these the backtest's trend filter reads the same bars as live trading; without them it
    falls back to resampling the base timeframe.
    """
    out = {}
    for symbol, timeframes in trend_timeframes(config).items():
        for timeframe in timeframes:
            path = bars_path(Path(data_dir), symbol, timeframe)
            if path.is_file():
                out[(symbol, timeframe)] = load_bars(path)
    return out


def save_spec(data_dir: Path, spec: SymbolSpec) -> Path:
    path = data_dir / f"{spec.name}.spec.json"
    path.write_text(json.dumps(asdict(spec), indent=2), encoding="utf-8")
    return path


def load_spec(data_dir: Path, symbol: str) -> SymbolSpec:
    path = data_dir / f"{symbol}.spec.json"
    if path.is_file():
        return SymbolSpec(**json.loads(path.read_text(encoding="utf-8")))
    return preset_spec(symbol)


def preset_spec(symbol: str) -> SymbolSpec:
    """Typical contract specs when no broker export is available. Prefer `tradebot fetch` specs."""
    s = symbol.upper()
    if s.startswith(("XAU", "GOLD")):
        return SymbolSpec(symbol, 2, 0.01, 0.01, 1.0, 100, 0.01, 100, 0.01, 0, "XAU", "USD")
    if s.startswith(("XAG", "SILVER")):
        return SymbolSpec(symbol, 3, 0.001, 0.001, 5.0, 5000, 0.01, 100, 0.01, 0, "XAG", "USD")
    if s.startswith(("BTC", "ETH", "SOL", "LTC", "XRP", "ADA", "LINK")):
        return SymbolSpec(symbol, 2, 0.01, 0.01, 0.01, 1, 0.01, 100, 0.01, 0, s[:3], "USD")
    base, quote = s[:3], s[3:6]
    if quote == "JPY":
        return SymbolSpec(symbol, 3, 0.001, 0.001, 0.67, 100_000, 0.01, 100, 0.01, 0, base, quote)
    return SymbolSpec(symbol, 5, 0.00001, 0.00001, 1.0, 100_000, 0.01, 100, 0.01, 0, base, quote)


def synthetic_bars(
    n: int = 20_000,
    seed: int = 7,
    start: str = "2024-01-01",
    minutes: int = 15,
    price: float = 1.10,
    volatility: float = 0.0006,
    trend_strength: float = 0.00012,
    regime_length: int = 600,
    spread_points: int = 8,
) -> pd.DataFrame:
    """A random walk that alternates between trending and ranging regimes.

    Used by the tests and the demo: it has enough structure for trend-following logic to find
    trades, and a fixed seed makes every run reproducible. It is not a model of real markets.
    """
    rng = np.random.default_rng(seed)
    regimes = np.repeat(rng.choice([-1, 0, 0, 1], size=n // regime_length + 1), regime_length)[:n]
    drift = regimes * trend_strength
    shocks = rng.standard_t(df=4, size=n) * volatility / np.sqrt(2)
    log_close = np.log(price) + np.cumsum(drift + shocks)
    close = np.exp(log_close)
    open_ = np.concatenate([[price], close[:-1]])
    wick = np.abs(rng.normal(0, volatility * 0.6, size=(2, n))) * close
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    times = pd.date_range(start, periods=n * 2, freq=f"{minutes}min")
    times = times[times.day_of_week < 5][:n]  # weekdays only, like FX
    return pd.DataFrame(
        {
            "time": times,
            "open": open_.round(5),
            "high": high.round(5),
            "low": low.round(5),
            "close": close.round(5),
            "spread": spread_points,
        }
    )
