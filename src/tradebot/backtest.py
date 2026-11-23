"""Event-driven backtester.

Bars from every symbol are merged into one timeline ordered by close time. For each moment a bar
closes, the simulated broker first triggers the stops/targets inside every symbol's bar that
closes then, then the *live* ``Engine`` makes its decision for each, exactly as it would in
production. Indicators are computed once per symbol
(vectorised, causal) and the per-bar loop only does the engine work, which keeps multi-year,
multi-symbol tests to seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from tradebot.broker.base import SymbolSpec
from tradebot.broker.sim import ClosedTrade, SimBroker
from tradebot.config import BotConfig
from tradebot.engine import Engine
from tradebot.metrics import Metrics, compute_metrics, per_symbol
from tradebot.strategy import compute_signals

DATA_HOLE = pd.Timedelta(days=7)
"""A longer pause between two of a symbol's bars is missing history, not a weekend or a holiday
(the longest of those, Easter for stocks, is under five days)."""

_ROW_COLUMNS = [
    "time",
    "open",
    "high",
    "low",
    "close",
    "spread",
    "atr",
    "signal",
    "raw_signal",
    "rsi",
    "adx",
    "trend",
    "block",
]


_OPTIONAL_COLUMNS = ("target_price", "exit_signal", "join_signal", "join_from")
"""Setup targets and the pre-quality exit signal; older supplied frames may omit them."""


@dataclass
class BacktestResult:
    config: BotConfig
    symbols: list[str]
    start: datetime
    end: datetime
    trades: list[ClosedTrade]
    equity: pd.Series
    metrics: Metrics
    per_symbol: pd.DataFrame
    signals: int
    skips: dict[str, int] = field(default_factory=dict)


def prepare_features(
    config: BotConfig,
    data: dict[str, pd.DataFrame],
    htf: dict[tuple[str, str], pd.DataFrame] | None = None,
) -> dict[str, pd.DataFrame]:
    """Signals per symbol. ``htf`` holds real higher-timeframe bars keyed by (symbol, timeframe)."""
    htf = htf or {}
    out = {}
    for symbol, bars in data.items():
        params = config.for_symbol(symbol)
        strategy = params.strategy
        frames = {
            tf: htf[(symbol, tf)] for tf in strategy.trend_ladder(config.timeframe) if (symbol, tf) in htf
        }
        out[symbol] = compute_signals(bars, strategy, config.timeframe, frames, params.risk.market_hours)
    return out


def run_backtest(
    config: BotConfig,
    data: dict[str, pd.DataFrame],
    specs: dict[str, SymbolSpec],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    features: dict[str, pd.DataFrame] | None = None,
    htf: dict[tuple[str, str], pd.DataFrame] | None = None,
) -> BacktestResult:
    """Backtest ``config`` on ``data`` (closed bars per symbol), optionally within [start, end)."""
    features = features if features is not None else prepare_features(config, data, htf)
    frames = []
    for symbol, frame in features.items():
        window = frame
        if start is not None:
            window = window[window["time"] >= start]
        if end is not None:
            window = window[window["time"] < end]
        if "spread" not in window:
            window = window.assign(spread=0)
        columns = [*_ROW_COLUMNS, *(c for c in _OPTIONAL_COLUMNS if c in window.columns)]
        frames.append(window[columns].assign(symbol=symbol))
    timeline = pd.concat(frames, ignore_index=True).sort_values(["time", "symbol"], kind="stable")
    if timeline.empty:
        raise ValueError("no bars in the requested backtest window")

    params = {s: config.for_symbol(s) for s in features}
    initial = config.backtest.initial_balance
    sim = SimBroker(specs, {s: p.backtest for s, p in params.items()}, initial)
    engine = Engine(config, sim, execute=True, live=False)
    step = pd.Timedelta(minutes=config.bar_minutes)

    current: pd.Timestamp | None = None
    last_bar: dict[str, pd.Timestamp] = {}
    sim.equity_curve.append((timeline["time"].iloc[0], initial))
    signals = 0
    rows = timeline.to_dict("records")
    i = 0
    while i < len(rows):
        # Every symbol's bar that closes at this moment first: its stops and targets fill and its
        # price is marked, so a decision on one symbol sees the others as they are at that close,
        # not a bar behind (renaming a symbol changed results: audit of 19 Nov 2026).
        close_time = rows[i]["time"] + step
        group = []
        while i < len(rows) and rows[i]["time"] + step == close_time:
            group.append(rows[i])
            i += 1
        if current is not None:
            sim.record_equity(current)
        current = close_time
        for row in group:
            symbol = row["symbol"]
            before = last_bar.get(symbol)
            if before is not None and row["time"] - before > DATA_HOLE:
                # No prices for weeks: what happened to an open trade is unknown. It leaves at the
                # last price seen, not at whatever the first bar after the hole says (a META trade
                # "held" through a 3-month hole in the stock history booked +8R that never was).
                sim.close_symbol(symbol, "data hole")
            last_bar[symbol] = row["time"]
            sim.start_bar(symbol, row, close_time)
        for row in group:  # simultaneous new entries still compete in symbol order
            symbol = row["symbol"]
            join = row.get("join_signal", 0)
            if row["signal"] != 0:
                signals += 1
            elif not (join and pd.notna(join)) and not any(
                p.symbol == symbol for p in sim.positions(config.magic)
            ):
                continue  # nothing to decide (a join is decided while flat, so it must not be skipped)
            engine.on_bar(symbol, specs[symbol], params[symbol], row, sim.spread(symbol, row), close_time)
    sim.close_all()
    if current is not None:  # the timeline is not empty, so there was a bar
        sim.record_equity(current)

    equity = pd.Series(dict(sim.equity_curve)).sort_index()
    equity = equity[~equity.index.duplicated(keep="last")]
    trades = sorted(sim.trades, key=lambda t: t.exit_time)
    return BacktestResult(
        config=config,
        symbols=list(features),
        start=timeline["time"].iloc[0].to_pydatetime(),
        end=(timeline["time"].iloc[-1] + step).to_pydatetime(),
        trades=trades,
        equity=equity,
        metrics=compute_metrics(equity, trades, initial),
        per_symbol=per_symbol(trades),
        signals=signals,
        skips=dict(engine.skips),
    )
