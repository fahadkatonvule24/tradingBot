"""Can the bot tell a false signal from a good one? Measured, not assumed.

    pip install -e ".[research]"
    python scripts/research_false_signals.py

A false signal here is a trade a setup takes that ends below zero R after the engine's own exits,
costs and management. Three questions, on the H1 history of the six instruments:

1. The entry-quality filter (strategy.use_signal_quality_filter, switched on in config.json on
   16 Nov 2026): the trades it removes against the trades it keeps, per setup and per year.
2. Single measurements at the signal bar (how far past the level it closed, candle shape, stretch
   from the averages, momentum, volatility regime, trend strength, hour): does any of them
   separate losers from winners in the research years, consistently from year to year?
3. All of them together: a logistic regression refitted each year on the years before it, so
   every prediction is out of sample. Its AUC, and the R of the signals it would have rejected.

Questions 2 and 3 are fitted and judged on the research years before HOLDOUT_START only. The
holdout has been used four times already (README), so it is reported for question 1 alone: the
filter is already switched on, and nothing here is tuned on it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from research_families import DATA, HOLDOUT_START

from tradebot.backtest import prepare_features, run_backtest
from tradebot.config import load_config
from tradebot.data import load_dataset, load_trend_bars
from tradebot.signal_quality import add_quality_features

OUT = Path("reports") / "research_false_signals.json"
SETUPS = ("ema_cross", "breakout", "pullback", "retest")
FEATURES = (
    "break_margin",  # close beyond the prior 20-bar extreme, ATR, in the trade's direction
    "body",  # candle body / range, in the trade's direction
    "close_position",  # where the close sits in the candle, 1 = at the extreme in the trade's direction
    "range_atr",  # the signal candle's true range / the previous bar's ATR
    "stretch_fast",  # close - EMA 10, ATR, in the trade's direction
    "stretch_slow",  # close - EMA 50, ATR, in the trade's direction
    "efficiency",  # net move / path over the prior 20 bars
    "adx",
    "rsi_dir",  # RSI - 50, in the trade's direction
    "macd_dir",  # MACD histogram / ATR, in the trade's direction
    "atr_rank",  # ATR's percentile over the previous 500 bars: the volatility regime
    "london_ny",  # 1 when the decision falls 12:00-16:00 UTC (both centres open)
)


def signal_rows(features: dict[str, pd.DataFrame], trades: list, bar: pd.Timedelta) -> pd.DataFrame:
    """One row per trade: its R and the measurements on the bar that triggered it."""
    rows = []
    for symbol, df in features.items():
        df = df.copy()
        add_quality_features(df, replace(_P, quality_period=20))
        df["atr_rank"] = df["atr"].rolling(500, min_periods=200).rank(pct=True).shift(1)
        df["prior_high"] = df["high"].rolling(20).max().shift(1)
        df["prior_low"] = df["low"].rolling(20).min().shift(1)
        index = pd.Index(df["time"])
        for t in (t for t in trades if t.symbol == symbol):
            at = index.get_indexer([pd.Timestamp(t.entry_time) - bar])[0]
            if at < 0:
                continue
            r, s = df.iloc[at], t.side
            atr = r["atr"]
            span = r["quality_close"]
            rows.append(
                {
                    "symbol": symbol,
                    "time": pd.Timestamp(t.entry_time),
                    "side": s,
                    "r": t.r_multiple,
                    "win": int(t.r_multiple > 0),
                    "break_margin": (
                        (r["close"] - r["prior_high"]) if s > 0 else (r["prior_low"] - r["close"])
                    )
                    / atr,
                    "body": r["quality_body"] * s,
                    "close_position": span if s > 0 else 1 - span,
                    "range_atr": r["quality_range_atr"],
                    "stretch_fast": (r["close"] - r["ema_fast"]) * s / atr,
                    "stretch_slow": (r["close"] - r["ema_slow"]) * s / atr,
                    "efficiency": r["quality_efficiency"],
                    "adx": r["adx"],
                    "rsi_dir": (r["rsi"] - 50) * s,
                    "macd_dir": r["macd_hist"] * s / atr,
                    "atr_rank": r["atr_rank"],
                    "london_ny": int(12 <= pd.Timestamp(t.entry_time).hour < 16),
                }
            )
    return pd.DataFrame(rows)


def summary(r: pd.Series) -> dict:
    if not len(r):
        return {"trades": 0}
    return {"trades": len(r), "win_rate": round(float((r > 0).mean()) * 100, 1),
            "avg_r": round(float(r.mean()), 3), "total_r": round(float(r.sum()), 1)}  # fmt: skip


def quality_filter(base, data, specs, htf) -> dict:
    """Question 1: what the shipped filter removes, against what it keeps."""
    out = {}
    for name in SETUPS:
        off = run_backtest(
            base.with_strategy(name=name, use_signal_quality_filter=False), data, specs, htf=htf
        )
        on = run_backtest(base.with_strategy(name=name, use_signal_quality_filter=True), data, specs, htf=htf)
        kept_keys = {(t.symbol, t.entry_time) for t in on.trades}
        frame = pd.DataFrame(
            {
                "time": pd.Timestamp(t.entry_time),
                "r": t.r_multiple,
                "kept": (t.symbol, t.entry_time) in kept_keys,
            }
            for t in off.trades
        )
        research = frame[frame["time"] < HOLDOUT_START]
        holdout = frame[frame["time"] >= HOLDOUT_START]
        by_year = {
            int(year): {
                "kept_avg_r": round(float(g.loc[g["kept"], "r"].mean()), 3),
                "removed_avg_r": round(float(g.loc[~g["kept"], "r"].mean()), 3)
                if (~g["kept"]).any()
                else None,
            }
            for year, g in research.groupby(research["time"].dt.year)
        }
        better = [
            v["removed_avg_r"] < v["kept_avg_r"] for v in by_year.values() if v["removed_avg_r"] is not None
        ]
        out[name] = {
            "off": {"trades": off.metrics.trades, "return_pct": round(off.metrics.return_pct, 1),
                    "avg_r": round(off.metrics.avg_r, 3), "win_rate": round(off.metrics.win_rate, 1)},
            "on": {"trades": on.metrics.trades, "return_pct": round(on.metrics.return_pct, 1),
                   "avg_r": round(on.metrics.avg_r, 3), "win_rate": round(on.metrics.win_rate, 1)},
            "research_removed": summary(research.loc[~research["kept"], "r"]),
            "research_kept": summary(research.loc[research["kept"], "r"]),
            "holdout_removed": summary(holdout.loc[~holdout["kept"], "r"]),
            "holdout_kept": summary(holdout.loc[holdout["kept"], "r"]),
            "years_removed_worse": f"{sum(better)} of {len(better)}",
            "by_year": by_year,
        }  # fmt: skip
        print(name, json.dumps({k: v for k, v in out[name].items() if k != "by_year"}), flush=True)
    return out


def single_measurements(rows: pd.DataFrame) -> dict:
    """Question 2: rank correlation of each measurement with R, overall and year by year."""
    out = {}
    years = rows["time"].dt.year
    for feature in FEATURES:
        x = rows[feature]
        ok = x.notna()
        overall = float(x[ok].rank().corr(rows.loc[ok, "r"].rank()))
        yearly = []
        for _year, g in rows[ok].groupby(years[ok]):
            if len(g) >= 30:
                yearly.append(float(g[feature].rank().corr(g["r"].rank())))
        same = sum(np.sign(c) == np.sign(overall) for c in yearly)
        quintile = pd.qcut(x[ok].rank(method="first"), 5, labels=False)
        by_q = rows.loc[ok].groupby(quintile)["r"].mean().round(3).tolist()
        out[feature] = {"spearman": round(overall, 3), "same_sign_years": f"{same} of {len(yearly)}",
                        "avg_r_by_quintile": by_q}  # fmt: skip
    return out


def model(rows: pd.DataFrame) -> dict:
    """Question 3: logistic regression refitted every year on the years before it."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    data = rows.dropna(subset=list(FEATURES)).copy()
    data["year"] = data["time"].dt.year
    predictions = []
    for year in sorted(data["year"].unique()):
        train, test = data[data["year"] < year], data[data["year"] == year]
        if len(train) < 300 or train["win"].nunique() < 2:
            continue
        fit = make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=1000))
        fit.fit(train[list(FEATURES)], train["win"])
        predictions.append(test.assign(p_win=fit.predict_proba(test[list(FEATURES)])[:, 1]))
    if not predictions:
        return {}
    oos = pd.concat(predictions)
    auc = float(roc_auc_score(oos["win"], oos["p_win"]))
    cut = oos["p_win"].quantile(0.3)
    rejected, kept = oos[oos["p_win"] <= cut], oos[oos["p_win"] > cut]
    deciles = oos.groupby(pd.qcut(oos["p_win"].rank(method="first"), 10, labels=False))["r"].mean()
    return {
        "out_of_sample_years": [int(y) for y in sorted(oos["year"].unique())],
        "trades": len(oos),
        "auc": round(auc, 3),
        "reject_30pct_least_likely": summary(rejected["r"]),
        "keep_the_rest": summary(kept["r"]),
        "avg_r_by_decile_of_predicted_win": deciles.round(3).tolist(),
    }


_P = None


def main() -> None:
    global _P
    base = load_config("config/config.json")
    data, specs = load_dataset(DATA, list(base.symbols), base.timeframe)
    htf = load_trend_bars(DATA, base)
    bar = pd.Timedelta(minutes=base.bar_minutes)
    results: dict = {
        "holdout_start": str(HOLDOUT_START.date()),
        "quality_filter": quality_filter(base, data, specs, htf),
    }
    pooled = []
    for name in SETUPS:
        config = base.with_strategy(name=name, use_signal_quality_filter=False)
        _P = config.strategy
        trades = run_backtest(config, data, specs, htf=htf).trades
        rows = signal_rows(prepare_features(config, data, htf), trades, bar)
        rows = rows[rows["time"] < HOLDOUT_START]
        rows["setup"] = name
        pooled.append(rows)
        results[name] = {"trades": len(rows), "single": single_measurements(rows), "model": model(rows)}
        print(name, "model", json.dumps(results[name]["model"]), flush=True)
    everything = pd.concat(pooled, ignore_index=True)
    results["all_setups"] = {"trades": len(everything), "single": single_measurements(everything),
                             "model": model(everything)}  # fmt: skip
    print("all setups model", json.dumps(results["all_setups"]["model"]), flush=True)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
