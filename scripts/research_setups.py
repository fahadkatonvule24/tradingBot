"""Which setup suits each instrument, measured with the costs of the account that trades it.

Every setup on every configured instrument (Anthropic has too little history), each instrument
alone so no portfolio limit changes its trades. Costs are those of the Exness Standard account,
read from the terminal on 2026-11-21: no commission, the typical H1 spread as the floor under
the recorded one (the forex and gold history came from another feed with tighter spreads), and
swap per night at today's rates with the broker's triple day. Joins are off here: setups first.

Two periods: forex and gold 2017-2022 and 2023 to date; stocks (history from April 2024) 2025
and 2026 to date. A setup is fit to trade an instrument when it
  - has at least 15 trades in each period,
  - made money (average R above zero) in each period, and
  - still made money without its two best trades (a gap or two must not be the whole result).
Of those, the one with the best worse period is chosen. An instrument with none is signals only,
on the setup whose worse period lost least. With 56 pairs tried, luck alone passes some: the
bot's scorecard decides in the end.

Then joins: on the chosen trading setups, joins stay on only if they added R in both periods.

Run from the repository root: .venv\\Scripts\\python scripts/research_setups.py
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.backtest import prepare_features, run_backtest
from tradebot.config import STRATEGIES, BotConfig, load_config
from tradebot.data import load_bars, load_spec, trend_timeframes

DATA = Path("data")
OUT = Path("reports")
MIN_TRADES = 15
SIGNAL_MIN_TRADES = 10
FOREX_PERIODS = {"A": ("2017-01-01", "2023-01-01"), "B": ("2023-01-01", "2027-01-01")}
STOCK_PERIODS = {"A": ("2025-01-01", "2026-01-01"), "B": ("2026-01-01", "2027-01-01")}
STOCKS = {"AAPL", "AMD", "META", "NVDA", "MSFT", "AMZN", "GOOGL", "TSLA"}

# Exness Standard, read from the terminal on 2026-11-21. Spread: median H1 spread in points of
# the stored history's own point size (gold is stored with 2 digits: $0.24 = 24). Swap: USD per
# lot per night, long and short. Stocks keep the recorded spread (their history is Exness's).
SPREAD = {"EURUSD": 8, "GBPUSD": 10, "USDJPY": 10, "AUDUSD": 9, "USDCAD": 14, "XAUUSD": 24}
SWAP = {
    "EURUSD": (-5.50, 0.0), "GBPUSD": (-1.40, -1.40), "USDJPY": (0.0, 0.0), "AUDUSD": (0.0, -2.40),
    "USDCAD": (0.0, -5.44), "XAUUSD": (-43.16, 0.0), "AAPL": (-6.20, -5.10), "AMD": (-7.90, -6.50),
    "META": (-10.30, -8.50), "NVDA": (-2.50, -3.10), "MSFT": (-8.80, -7.30), "AMZN": (-4.50, -3.70),
    "GOOGL": (-4.90, -4.10), "TSLA": (-6.40, -5.20),
}  # fmt: skip


def costs(symbol: str) -> dict:
    """The ``backtest`` override for ``symbol``: the account's own costs."""
    long, short = SWAP[symbol]
    out = {"commission_per_lot": 0.0, "swap_long": long, "swap_short": short,
           "swap_triple_day": 4 if symbol in STOCKS else 2}  # fmt: skip
    if symbol in SPREAD:
        out["min_spread_points"] = SPREAD[symbol]
    return out


def single(base: BotConfig, symbol: str, setup: str, join: bool) -> BotConfig:
    config = replace(base, symbols=(symbol,), overrides={symbol: {"backtest": costs(symbol)}}).with_setup(
        setup
    )
    config = config.with_strategy(auto_join=join)
    config.validate()
    return config


def r_of(trades) -> np.ndarray:
    return np.array([t.r_multiple for t in trades], dtype=float)


def measure(base: BotConfig) -> tuple[pd.DataFrame, dict]:
    rows, trades_by = [], {}
    for symbol in [s for s in base.symbols if SWAP.get(s)]:
        periods = STOCK_PERIODS if symbol in STOCKS else FOREX_PERIODS
        bars = load_bars(DATA / f"{symbol}_{base.timeframe}.csv")
        specs = {symbol: load_spec(DATA, symbol)}
        for setup in STRATEGIES:
            joined = single(base, symbol, setup, join=True)  # join columns computed; used or not
            htf = {(symbol, tf): load_bars(DATA / f"{symbol}_{tf}.csv")
                   for tf in trend_timeframes(joined).get(symbol, ())}  # fmt: skip
            features = prepare_features(joined, {symbol: bars}, htf)
            for join in (False, True):
                config = joined if join else single(base, symbol, setup, join=False)
                for period, (start, end) in periods.items():
                    result = run_backtest(config, {symbol: bars}, specs, start=pd.Timestamp(start),
                                          end=pd.Timestamp(end), features=features)  # fmt: skip
                    r = r_of(result.trades)
                    trades_by[(symbol, setup, join, period)] = r
                    rows.append({"symbol": symbol, "setup": setup, "join": join, "period": period,
                                 "trades": len(r), "avg_r": r.mean() if len(r) else np.nan,
                                 "total_r": r.sum()})  # fmt: skip
            print(f"  {symbol} {setup} done", flush=True)
    return pd.DataFrame(rows), trades_by


def decide(table: pd.DataFrame, trades_by: dict) -> tuple[pd.DataFrame, dict]:
    plain = table[~table["join"]]
    verdicts = []
    for (symbol, setup), g in plain.groupby(["symbol", "setup"]):
        a, b = g.set_index("period").loc["A"], g.set_index("period").loc["B"]
        pooled = np.concatenate([trades_by[(symbol, setup, False, p)] for p in ("A", "B")])
        trimmed = (
            (pooled.sum() - np.sort(pooled)[-2:].sum()) / (len(pooled) - 2) if len(pooled) > 2 else np.nan
        )
        enough = min(a["trades"], b["trades"]) >= MIN_TRADES
        worse = min(a["avg_r"], b["avg_r"])
        t = pooled.mean() / (pooled.std(ddof=1) / np.sqrt(len(pooled))) if len(pooled) > 2 else np.nan
        verdicts.append({"symbol": symbol, "setup": setup, "trades_A": a["trades"], "avg_A": a["avg_r"],
                         "trades_B": b["trades"], "avg_B": b["avg_r"], "without_best_2": trimmed, "t": t,
                         "fit": bool(enough and worse > 0 and trimmed > 0), "worse": worse})  # fmt: skip
    v = pd.DataFrame(verdicts)
    return v, choose(v)


def choose(v: pd.DataFrame) -> dict:
    """Per symbol: the fit setup with the best worse period (it trades), else, for its signals, the
    setup whose worse period lost least among those with a handful of trades in each period (a
    setup with 3 trades proves nothing either way)."""
    choice = {}
    v = v.assign(worse=v[["avg_A", "avg_B"]].min(axis=1))
    for symbol, g in v.groupby("symbol"):
        fit = g[g["fit"]]
        if len(fit):
            pick, trade = fit.sort_values("worse", ascending=False).iloc[0], True
        else:
            some = g[g[["trades_A", "trades_B"]].min(axis=1) >= SIGNAL_MIN_TRADES]
            pick, trade = (some if len(some) else g).sort_values("worse", ascending=False).iloc[0], False
        choice[symbol] = {"setup": pick["setup"], "auto_trade": trade}
    return choice


def joins(table: pd.DataFrame, choice: dict) -> dict:
    traded = [(s, c["setup"]) for s, c in choice.items() if c["auto_trade"]]
    added = {}
    for period in ("A", "B"):
        total = 0.0
        for symbol, setup in traded:
            g = table[(table.symbol == symbol) & (table.setup == setup) & (table.period == period)]
            total += float(g[g["join"]]["total_r"].iloc[0] - g[~g["join"]]["total_r"].iloc[0])
        added[period] = total
    return added


def main() -> None:
    base = load_config("config/config.json")
    table, trades_by = measure(base)
    verdicts, choice = decide(table, trades_by)
    added = joins(table, choice)
    OUT.mkdir(exist_ok=True)
    table.to_csv(OUT / "research_setups.csv", index=False)
    verdicts.to_csv(OUT / "research_setups_verdicts.csv", index=False)
    keep_joins = all(v > 0 for v in added.values())
    (OUT / "research_setups.json").write_text(json.dumps({"choice": choice, "joins_added_r": added,
                                                          "auto_join": keep_joins}, indent=2))  # fmt: skip
    pd.set_option("display.width", 200)
    print(verdicts.round(3).drop(columns="worse").to_string(index=False))
    print()
    for symbol, c in choice.items():
        print(f"  {symbol:7} {c['setup']:9} {'TRADE' if c['auto_trade'] else 'signals only'}")
    print(f"\nJoins on the traded setups added {added['A']:+.1f}R (A) and {added['B']:+.1f}R (B): "
          f"auto_join {'stays on' if keep_joins else 'goes off'}")  # fmt: skip


if __name__ == "__main__":
    main()
