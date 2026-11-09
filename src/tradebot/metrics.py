"""Performance statistics computed from an equity curve and a list of closed trades."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from tradebot.broker.sim import ClosedTrade


@dataclass(frozen=True)
class Metrics:
    start_balance: float
    end_balance: float
    net_profit: float
    return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    max_drawdown_days: float
    sharpe: float
    sortino: float
    calmar: float
    trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    """Average net P/L per trade, account currency."""
    avg_r: float
    avg_win: float
    avg_loss: float
    best_trade: float
    worst_trade: float
    longest_losing_streak: int
    avg_bars_held: float
    years: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def drawdown(equity: pd.Series) -> pd.Series:
    """Fractional drawdown from the running peak (0 = at a new high, -0.1 = 10% below it)."""
    peak = equity.cummax()
    return equity / peak - 1


def compute_metrics(equity: pd.Series, trades: Sequence[ClosedTrade], start_balance: float) -> Metrics:
    equity = equity.dropna()
    end_balance = float(equity.iloc[-1]) if len(equity) else start_balance
    span_days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400 if len(equity) > 1 else 0.0
    years = span_days / 365.25
    total_return = end_balance / start_balance - 1
    cagr = (end_balance / start_balance) ** (1 / years) - 1 if years > 0.05 and end_balance > 0 else 0.0

    dd = drawdown(equity) if len(equity) else pd.Series(dtype=float)
    max_dd = float(-dd.min()) if len(dd) else 0.0
    max_dd_days = _longest_underwater_days(dd)

    daily = equity.resample("1D").last().dropna()
    returns = daily.pct_change().dropna()
    sharpe = sortino = 0.0
    if len(returns) > 2 and returns.std() > 0:
        sharpe = float(returns.mean() / returns.std() * math.sqrt(252))
        downside = returns[returns < 0].std()
        sortino = float(returns.mean() / downside * math.sqrt(252)) if downside and downside > 0 else 0.0

    pnl = np.array([t.pnl for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_loss = -losses.sum()
    profit_factor = float(wins.sum() / gross_loss) if gross_loss > 0 else (math.inf if len(wins) else 0.0)

    return Metrics(
        start_balance=start_balance,
        end_balance=end_balance,
        net_profit=end_balance - start_balance,
        return_pct=total_return * 100,
        cagr_pct=cagr * 100,
        max_drawdown_pct=max_dd * 100,
        max_drawdown_days=max_dd_days,
        sharpe=sharpe,
        sortino=sortino,
        calmar=(cagr / max_dd) if max_dd > 0 else 0.0,
        trades=len(trades),
        win_rate=float(len(wins) / len(trades) * 100) if trades else 0.0,
        profit_factor=profit_factor,
        expectancy=float(pnl.mean()) if len(pnl) else 0.0,
        avg_r=float(np.mean([t.r_multiple for t in trades])) if trades else 0.0,
        avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        best_trade=float(pnl.max()) if len(pnl) else 0.0,
        worst_trade=float(pnl.min()) if len(pnl) else 0.0,
        longest_losing_streak=_longest_streak(pnl <= 0),
        avg_bars_held=float(np.mean([t.bars_held for t in trades])) if trades else 0.0,
        years=years,
    )


def _longest_streak(flags: np.ndarray) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def _longest_underwater_days(dd: pd.Series) -> float:
    if dd.empty:
        return 0.0
    longest = pd.Timedelta(0)
    start = None
    for when, value in dd.items():
        if value < 0 and start is None:
            start = when
        elif value >= 0 and start is not None:
            longest = max(longest, when - start)
            start = None
    if start is not None:
        longest = max(longest, dd.index[-1] - start)
    return longest.total_seconds() / 86400


def per_symbol(trades: Sequence[ClosedTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["trades", "win_rate", "net_profit", "profit_factor", "avg_r"])
    df = pd.DataFrame({"symbol": t.symbol, "pnl": t.pnl, "r": t.r_multiple} for t in trades)
    rows = {}
    for symbol, g in df.groupby("symbol"):
        gains, losses = g.pnl[g.pnl > 0].sum(), -g.pnl[g.pnl <= 0].sum()
        rows[symbol] = {
            "trades": len(g),
            "win_rate": (g.pnl > 0).mean() * 100,
            "net_profit": g.pnl.sum(),
            "profit_factor": gains / losses if losses > 0 else math.inf,
            "avg_r": g.r.mean(),
        }
    return pd.DataFrame.from_dict(rows, orient="index").sort_values("net_profit", ascending=False)


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    """Year x month table of percentage returns."""
    month_end = equity.resample("ME").last().dropna()
    previous = month_end.shift(1)
    previous.iloc[0] = equity.iloc[0]
    returns = (month_end / previous - 1) * 100
    table = pd.DataFrame(
        {"year": returns.index.year, "month": returns.index.month, "ret": returns.to_numpy()}
    )
    return table.pivot(index="year", columns="month", values="ret")
