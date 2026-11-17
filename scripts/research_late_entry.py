"""How late can a trend still be joined? The measurement behind ``advisor.JOIN_WINDOW_BARS``.

    python scripts/research_late_entry.py [--config config/config.json] [--data data]

Part 1 takes every signal the configured setups gave inside the trading hours and enters it
0, 1, 3, 5 and 10 bars late, with a fresh stop (sl_atr) and target (tp_atr) from the late entry
and about a pip of cost. First touched of stop or target decides; a bar that touches both is a
loss. Part 2 backtests three ways of joining a running trend without a fresh signal, through the
bot's own engine and filters (higher-timeframe trend, ADX, room to run).

Result on 2026-11-17 (H1, six symbols, 06-17 UTC): 0-5 bars late averaged -0.03R to +0.04R, the
same as on time; 10 bars late -0.06R (2017-22) and -0.10R (2023-26). The three join rules lost
0.07R to 0.19R a trade in both periods.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import cli, strategy
from tradebot.backtest import prepare_features
from tradebot.config import load_config
from tradebot.data import load_dataset, load_trend_bars

DELAYS = (0, 1, 3, 5, 10)


def late_entries(config_path: str, data_dir: Path) -> pd.DataFrame:
    cfg = load_config(config_path)
    data, specs = load_dataset(data_dir, list(cfg.symbols), cfg.timeframe)
    features = prepare_features(cfg, data, load_trend_bars(data_dir, cfg))
    rows = []
    for symbol, f in features.items():
        risk = cfg.for_symbol(symbol).risk
        high, low, close, atr = (f[k].to_numpy(float) for k in ("high", "low", "close", "atr"))
        cost = specs[symbol].point * 10
        for i in np.flatnonzero(f["signal"].to_numpy() != 0):
            closed = (f["time"].iat[i] + pd.Timedelta(minutes=cfg.bar_minutes)).to_pydatetime()
            if cfg.schedule.closed(closed):
                continue
            side = int(f["signal"].iat[i])
            for k in DELAYS:
                j = i + k
                if j >= len(close) - 1 or not np.isfinite(atr[j]):
                    continue
                sl, tp = risk.sl_atr * atr[j], risk.tp_atr * atr[j]
                entry = close[j] + side * cost
                stop, target = entry - side * sl, entry + side * tp
                for m in range(j + 1, min(j + 400, len(close))):
                    if (low[m] <= stop) if side > 0 else (high[m] >= stop):
                        r = -1.0
                        break
                    if (high[m] >= target) if side > 0 else (low[m] <= target):
                        r = tp / sl
                        break
                else:
                    continue
                period = "2023+" if f["time"].iat[i].year >= 2023 else "to 2022"
                rows.append({"bars_late": k, "period": period, "r": r})
    return pd.DataFrame(rows)


def _aligned(df: pd.DataFrame) -> pd.Series:
    return np.sign(df["ema_fast"] - df["ema_slow"])


def _ema10_touch(df: pd.DataFrame) -> pd.Series:
    d, o, h, lo, c, f = _aligned(df), df["open"], df["high"], df["low"], df["close"], df["ema_fast"]
    return ((d > 0) & (lo <= f) & (c > f) & (c > o)).astype(int) - (
        (d < 0) & (h >= f) & (c < f) & (c < o)
    ).astype(int)


def _ema50_zone(df: pd.DataFrame) -> pd.Series:
    d, o, h, lo, c, s, a = (
        _aligned(df),
        *(df[k] for k in ("open", "high", "low", "close", "ema_slow", "atr")),
    )
    up = (d > 0) & (lo <= s + 0.25 * a) & (c > s) & (c > o)
    down = (d < 0) & (h >= s - 0.25 * a) & (c < s) & (c < o)
    return up.astype(int) - down.astype(int)


def _flag_break(df: pd.DataFrame) -> pd.Series:
    d, h, lo, c = _aligned(df), df["high"], df["low"], df["close"]
    fell = (c.shift(1) < c.shift(2)) & (c.shift(2) < c.shift(3))
    rose = (c.shift(1) > c.shift(2)) & (c.shift(2) > c.shift(3))
    return ((d > 0) & fell & (c > h.shift(1))).astype(int) - ((d < 0) & rose & (c < lo.shift(1))).astype(int)


JOIN_RULES = {"dip to EMA 10": _ema10_touch, "dip to EMA 50": _ema50_zone, "flag break": _flag_break}


def join_rules(config_path: str) -> pd.DataFrame:
    """Each rule replaces the pullback setup's trigger, keeping its filters and the engine."""
    original, current = strategy._trigger, {}

    def trigger(df: pd.DataFrame, p) -> pd.Series:
        if p.name == "pullback" and current:
            return JOIN_RULES[current["rule"]](df).fillna(0).astype(int)
        return original(df, p)

    strategy._trigger = trigger
    rows = []
    try:
        for name in JOIN_RULES:
            current["rule"] = name
            for period, window in (("to 2022", ["--to", "2022-12-31"]), ("2023+", ["--from", "2023-01-01"])):
                with tempfile.TemporaryDirectory() as out, contextlib.redirect_stdout(io.StringIO()):
                    cli.main(["-c", config_path, "backtest", "--strategy", "pullback", *window, "-o", out])
                    trades = pd.read_csv(next(Path(out).glob("trades_*.csv")))
                rows.append(
                    {"rule": name, "period": period, "trades": len(trades), "avg_r": trades.r_multiple.mean()}
                )
    finally:
        strategy._trigger = original
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args()
    late = late_entries(args.config, args.data)
    print("Entering a signal late (average R per trade):")
    print(late.groupby(["bars_late", "period"]).r.agg(["count", "mean"]).round(3).unstack().to_string())
    print("\nJoining a running trend without a fresh signal:")
    print(
        join_rules(args.config)
        .pivot(index="rule", columns="period", values=["trades", "avg_r"])
        .round(3)
        .to_string()
    )


if __name__ == "__main__":
    main()
