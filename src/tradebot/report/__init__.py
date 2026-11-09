"""Self-contained HTML reports for backtests and walk-forward runs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, PackageLoader, select_autoescape

from tradebot import __version__
from tradebot.metrics import drawdown, monthly_returns
from tradebot.report import svg

if TYPE_CHECKING:
    from tradebot.backtest import BacktestResult
    from tradebot.optimize import WalkForwardResult


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("tradebot", "report/templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = lambda v: f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
    env.filters["pct"] = lambda v, d=2: f"{v:+.{d}f}%"
    env.filters["num"] = lambda v, d=2: "∞" if v == float("inf") else f"{v:,.{d}f}"
    env.filters["dt"] = lambda v, f="%Y-%m-%d %H:%M": v.strftime(f) if v else "—"
    return env


def _charts(equity, trades, per_symbol) -> dict:
    dd = drawdown(equity) * 100
    bars = [
        svg.Bar(
            str(sym),
            float(row["net_profit"]),
            f"{sym}: {row['net_profit']:+,.2f} over {int(row['trades'])} trades",
        )
        for sym, row in per_symbol.iterrows()
    ]
    return {
        "equity": svg.line(equity, "Equity curve", money=True),
        "drawdown": svg.line(dd, "Drawdown", money=False, height=170, baseline=0.0, area=True, alert=True),
        "symbols": svg.diverging_bars(bars, "Net profit by symbol"),
        "r_hist": svg.histogram([t.r_multiple for t in trades], "Distribution of trade results in R"),
        "monthly": svg.monthly_heatmap(monthly_returns(equity), "Monthly returns"),
    }


def render_backtest(
    result: BacktestResult, title: str | None = None, walk_forward: WalkForwardResult | None = None
) -> str:
    source = walk_forward if walk_forward is not None else result
    equity = walk_forward.oos_equity if walk_forward else result.equity
    trades = walk_forward.oos_trades if walk_forward else result.trades
    per_symbol = walk_forward.oos_per_symbol if walk_forward else result.per_symbol
    metrics = walk_forward.oos_metrics if walk_forward else result.metrics
    config = result.config
    return (
        _env()
        .get_template("backtest.html")
        .render(
            title=title or ("Walk-forward report" if walk_forward else "Backtest report"),
            version=__version__,
            generated=datetime.now(),
            config=config,
            config_json=json.dumps(config.to_dict(), indent=2, default=str),
            result=result,
            wf=walk_forward,
            m=metrics,
            trades=list(reversed(trades))[:60],
            trade_count=len(trades),
            per_symbol=per_symbol,
            charts=_charts(equity, trades, per_symbol),
            start=equity.index[0],
            end=equity.index[-1],
            source=source,
        )
    )


def write_report(html: str, out_dir: Path, name: str = "report.html") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(html, encoding="utf-8")
    return path
