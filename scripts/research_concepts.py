"""More chart concepts, measured as warnings on the bot's own signals and joins.

    python scripts/research_concepts.py [--config config/config.json] [--data data]

Same harness as ``research_smc.py`` part 1: every signal of all four setups, entered on time and
1 to 5 bars late, fresh ATR stop and target, about a pip of cost, first touch decides. Each entry
is marked with what held on its entry bar, and the marked entries are compared with the rest in
2017-2022 and 2023-2026 separately. A concept is worth acting on only if it separates the two
groups the same way in both periods.

  session        UTC hour the entry bar closed: Asia 23-07, London 07-12, London/New York 12-16,
                 New York 16-21, rollover 21-23 (spreads widen; the backtest's spread is fixed)
  premium/discount  where the close sits in the last ``sr_lookback`` bars' range: a buy above the
                 middle is "in premium", a sell below it "in discount" (ICT: buy cheap, sell dear)
  CHoCH against  the swing structure (``structure.structure_state``, swings as known on that bar)
                 is a trend the other way, or has just broken against the trade
  candle         the signal bar is an engulfing bar or a pin bar (rejection wick at least twice
                 the body, close in the outer third) in the trade's direction
  pool ahead     two or more untaken swing highs (for a buy; lows for a sell) within 0.1 ATR of
                 each other, between the entry and 1.5 ATR beyond it: equal highs, where stops
                 cluster, in the trade's way

Result on 2026-11-18 (H1, six symbols, all day), average R, 2017-2022 / 2023-2026, marked vs not:
  session          no consistent order: London was the worst session for joins in 2023+ (-0.10)
                   and among the best before (-0.03); Asia -0.04 / -0.08. Trading all day is not
                   measurably worse than set hours (the backtest's spread does not widen at rollover)
  premium/discount joins -0.03 / -0.07 against -0.06 / -0.05: reversed between periods
  CHoCH against    joins -0.05 / -0.03 against -0.03 / -0.08: reversed between periods
  candle           signals on time -0.02 / -0.09 against -0.05 / -0.06: reversed between periods
  pool ahead       joins -0.02 / +0.02 against -0.04 / -0.07, the only one the same way in both,
                   on about a thousand joins a period and a small gap before 2023; signals on time
                   reversed. With ten comparisons made, one like this is expected by chance, so it
                   changes nothing.
None of them is used by the bot.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.backtest import prepare_features
from tradebot.config import STRATEGIES, load_config
from tradebot.data import load_dataset, load_trend_bars
from tradebot.structure import structure_state

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smc_research = _load("research_smc")
structure_research = _load("research_structure")
DELAYS = smc_research.DELAYS


def session(hour: int) -> str:
    if hour >= 23 or hour < 7:
        return "Asia 23-07"
    if hour < 12:
        return "London 07-12"
    if hour < 16:
        return "overlap 12-16"
    if hour < 21:
        return "New York 16-21"
    return "rollover 21-23"


def per_bar(f: pd.DataFrame, lookback: int) -> dict[str, np.ndarray]:
    """Everything that does not depend on the trade, one value per bar, from closed bars only."""
    o, h, lo, c, atr = (f[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr"))
    n = len(f)
    top = pd.Series(h).rolling(lookback).max().to_numpy()
    bottom = pd.Series(lo).rolling(lookback).min().to_numpy()
    position = (c - bottom) / np.where(top > bottom, top - bottom, np.nan)
    body = np.abs(c - o)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - lo
    span = np.where(h > lo, h - lo, np.nan)
    prev_o, prev_c = np.roll(o, 1), np.roll(c, 1)
    bull_engulf = (c > o) & (prev_c < prev_o) & (c >= prev_o) & (o <= prev_c)
    bear_engulf = (c < o) & (prev_c > prev_o) & (c <= prev_o) & (o >= prev_c)
    bull_pin = (lower >= 2 * body) & ((c - lo) / span >= 2 / 3)
    bear_pin = (upper >= 2 * body) & ((h - c) / span >= 2 / 3)
    state = np.zeros(n, dtype=np.int8)  # +1 up, -1 down, 0 range/unknown
    broken = np.zeros(n, dtype=np.int8)
    pool_up = np.zeros(n, dtype=bool)
    pool_down = np.zeros(n, dtype=bool)
    for i, points in structure_research.incremental_swings(f):
        if len(points) >= 4:
            s = structure_state(points, c[i])
            state[i] = {"up": 1, "down": -1}.get(s["state"], 0)
            broken[i] = s["broken"]
        if not np.isfinite(atr[i]):
            continue
        # equal swing highs (lows) not yet traded through, between the close and 1.5 ATR beyond it
        highs = [
            p.price for p in points if p.kind == "high" and p.price > c[i] and p.price - c[i] <= 1.5 * atr[i]
        ]
        lows = [
            p.price for p in points if p.kind == "low" and p.price < c[i] and c[i] - p.price <= 1.5 * atr[i]
        ]
        tol = 0.1 * atr[i]
        pool_up[i] = any(abs(a - b) <= tol for k, a in enumerate(highs) for b in highs[k + 1 :])
        pool_down[i] = any(abs(a - b) <= tol for k, a in enumerate(lows) for b in lows[k + 1 :])
    return {"position": position, "bull_candle": bull_engulf | bull_pin, "bear_candle": bear_engulf | bear_pin,
            "state": state, "broken": broken, "pool_up": pool_up, "pool_down": pool_down}  # fmt: skip


def measure(config_path: str, data_dir: Path) -> pd.DataFrame:
    base = load_config(config_path)
    data, specs = load_dataset(data_dir, list(base.symbols), base.timeframe)
    trend_bars = load_trend_bars(data_dir, base)
    cached: dict[str, dict[str, np.ndarray]] = {}
    rows = []
    for setup in STRATEGIES:
        cfg = base.with_setup(setup)
        for symbol, f in prepare_features(cfg, data, trend_bars).items():
            if symbol not in cached:
                cached[symbol] = per_bar(f, cfg.for_symbol(symbol).strategy.sr_lookback)
            bars, risk = cached[symbol], cfg.for_symbol(symbol).risk
            high, low, close, atr = (f[k].to_numpy(float) for k in ("high", "low", "close", "atr"))
            times = f["time"]
            cost = specs[symbol].point * 10
            for i in np.flatnonzero(f["signal"].to_numpy() != 0):
                closed = (times.iat[i] + pd.Timedelta(minutes=cfg.bar_minutes)).to_pydatetime()
                if cfg.schedule.closed(closed):
                    continue
                side = int(f["signal"].iat[i])
                for k in DELAYS:
                    j = i + k
                    r = smc_research._outcome(high, low, close, atr, j, side, risk.sl_atr, risk.tp_atr, cost)
                    if r is None:
                        continue
                    at = (times.iat[j] + pd.Timedelta(minutes=cfg.bar_minutes)).hour
                    pos = bars["position"][j]
                    rows.append({
                        "setup": setup, "entry": "on time" if k == 0 else "join 1-5",
                        "period": smc_research._period(times.iat[i]), "r": r, "session": session(at),
                        "premium/discount": bool(np.isfinite(pos) and (pos > 0.5 if side > 0 else pos < 0.5)),
                        "CHoCH against": bool(bars["state"][j] == -side or bars["broken"][j] == -side),
                        "candle": bool((bars["bull_candle"] if side > 0 else bars["bear_candle"])[i]),
                        "pool ahead": bool((bars["pool_up"] if side > 0 else bars["pool_down"])[j]),
                    })  # fmt: skip
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args()
    pd.set_option("display.width", 200)
    marked = measure(args.config, args.data)
    for flag in ("session", "premium/discount", "CHoCH against", "candle", "pool ahead"):
        table = marked.groupby(["entry", flag, "period"]).r.agg(["count", "mean"]).round(3).unstack("period")
        print(f"--- {flag} ---\n{table.to_string()}\n")


if __name__ == "__main__":
    main()
