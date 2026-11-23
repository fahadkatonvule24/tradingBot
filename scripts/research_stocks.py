"""Do the bot's setups hold up on US stocks? Every setup on each stock, in two separate periods.

The broker's stock history starts in June 2023 but has a nine-month hole up to 2024-04-01 and a
three-month hole in mid-2026. The D1 trend filter's EMA 200 is warm from about January 2025, so
the two periods are calendar 2025 and 2026 to date. One stock per run, so the portfolio limits
of one stock never change another's trades.

Costs: the recorded spread (at least 10 points, $0.10 a share) and no commission (Exness
Standard charges stocks through the spread). Swap, about -$5 a lot a night, is left out: at the
0.03-0.10 lots these sizes come to, it is well under 0.01R a night.

A stock and setup "holds up" when it made money (average R above zero) in both periods with at
least 15 trades in each. With 32 pairs tried, a few will pass by luck: treat a pass as a reason
to trade it small and keep watching the bot's scorecard, not as proof.

Run from the repository root: .venv\\Scripts\\python scripts/research_stocks.py
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from tradebot.backtest import run_backtest
from tradebot.config import STRATEGIES, load_config
from tradebot.data import load_bars, load_spec, trend_timeframes

DATA = Path("data")
STOCKS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD")
PERIODS = {"2025": ("2025-01-01", "2026-01-01"), "2026": ("2026-01-01", "2027-01-01")}
MIN_TRADES = 15
COSTS = {"backtest": {"commission_per_lot": 0.0, "min_spread_points": 10}}


def measure() -> pd.DataFrame:
    base = load_config("config/config.json")
    rows = []
    for symbol in STOCKS:
        bars = load_bars(DATA / f"{symbol}_{base.timeframe}.csv")
        specs = {symbol: load_spec(DATA, symbol)}
        for setup in STRATEGIES:
            config = replace(base, symbols=(symbol,), overrides={symbol: COSTS}).with_setup(setup)
            config.validate()
            htf = {
                (symbol, tf): load_bars(DATA / f"{symbol}_{tf}.csv")
                for tf in trend_timeframes(config).get(symbol, ())
            }
            for period, (start, end) in PERIODS.items():
                result = run_backtest(
                    config, {symbol: bars}, specs, start=pd.Timestamp(start), end=pd.Timestamp(end), htf=htf
                )
                m = result.metrics
                rows.append({
                    "symbol": symbol, "setup": setup, "period": period, "trades": m.trades,
                    "avg_r": m.avg_r if m.trades else float("nan"), "win": m.win_rate,
                    "return_pct": m.return_pct, "max_dd_pct": m.max_drawdown_pct,
                })  # fmt: skip
    return pd.DataFrame(rows)


def verdict(table: pd.DataFrame) -> pd.DataFrame:
    wide = table.pivot_table(index=["symbol", "setup"], columns="period", values=["trades", "avg_r"])
    wide.columns = [f"{value}_{period}" for value, period in wide.columns]
    trades = wide[[f"trades_{p}" for p in PERIODS]]
    avg = wide[[f"avg_r_{p}" for p in PERIODS]]
    wide["holds_up"] = (trades.min(axis=1) >= MIN_TRADES) & (avg.min(axis=1) > 0)
    return wide.reset_index()


def main() -> None:
    table = measure()
    out = Path("reports")
    out.mkdir(exist_ok=True)
    table.to_csv(out / "research_stocks.csv", index=False)
    result = verdict(table)
    pd.set_option("display.width", 160)
    print(result.round(3).to_string(index=False))
    passed = result[result["holds_up"]]
    print(
        f"\n{len(passed)} of {len(result)} stock/setup pairs made money in both periods with {MIN_TRADES}+ trades each"
    )
    for setup, group in result.groupby("setup"):
        print(
            f"  {setup:<10} avg R 2025 {group['avg_r_2025'].mean():+.3f}  2026 {group['avg_r_2026'].mean():+.3f}"
        )


if __name__ == "__main__":
    main()
