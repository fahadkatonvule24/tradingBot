"""Event-driven backtester.

Bars from every symbol are merged into one timeline ordered by close time. For each closed bar
the simulated broker first triggers any stops/targets inside it, then the *live* ``Engine``
makes its decision exactly as it would in production. Indicators are computed once per symbol
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
    htf: dict[str, pd.DataFrame] | None = None,
) -> dict[str, pd.DataFrame]:
    htf = htf or {}
    return {
        symbol: compute_signals(bars, config.for_symbol(symbol).strategy, config.timeframe, htf.get(symbol))
        for symbol, bars in data.items()
    }


def run_backtest(
    config: BotConfig,
    data: dict[str, pd.DataFrame],
    specs: dict[str, SymbolSpec],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    features: dict[str, pd.DataFrame] | None = None,
    htf: dict[str, pd.DataFrame] | None = None,
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
        frames.append(window[_ROW_COLUMNS].assign(symbol=symbol))
    timeline = pd.concat(frames, ignore_index=True).sort_values(["time", "symbol"], kind="stable")
    if timeline.empty:
        raise ValueError("no bars in the requested backtest window")

    params = {s: config.for_symbol(s) for s in features}
    initial = config.backtest.initial_balance
    sim = SimBroker(specs, {s: p.backtest for s, p in params.items()}, initial)
    engine = Engine(config, sim, execute=True, live=False)
    step = pd.Timedelta(minutes=config.bar_minutes)

    current: pd.Timestamp | None = None
    sim.equity_curve.append((timeline["time"].iloc[0], initial))
    signals = 0
    for row in timeline.to_dict("records"):
        symbol = row["symbol"]
        close_time = row["time"] + step
        if current is not None and close_time != current:
            sim.record_equity(current)
        current = close_time
        sim.start_bar(symbol, row, close_time)
        if row["signal"] != 0:
            signals += 1
        elif not any(p.symbol == symbol for p in sim.positions(config.magic)):
            continue  # nothing to decide on this bar
        engine.on_bar(symbol, specs[symbol], params[symbol], row, sim.spread(symbol, row), close_time)
    sim.close_all()
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
