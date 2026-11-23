"""What liquidity sweeps, fair value gaps and supply/demand zones (``tradebot.smc``) are worth.

    python scripts/research_smc.py [--config config/config.json] [--data data]

Part 1, the cautions: every signal of each setup, entered on time and 1 to 5 bars late (the join
window), with a fresh stop (sl_atr) and target (tp_atr) and about a pip of cost; first touched of
stop or target decides, a bar touching both is a loss (as in ``research_late_entry.py``). Each
entry is marked with the cautions that held on its entry bar (``smc.against``): a sweep the other
way in the last few bars, price at an opposite supply/demand zone, price at an opposite fair value
gap. If the cautions mean anything, the marked entries do worse than the unmarked ones, in both
periods.

Part 2, the concepts as entries: a sweep (enter the way it reclaimed), the first test of a fair
value gap closing its way, and the first touch of a fresh zone closing back out of it, each
replacing the pullback setup's trigger, through the bot's engine, stops and costs; first without
the setup's filters and then with them (higher-timeframe trend, ADX, room to run).

Risk per trade is set to 0.25% for the measurement so the daily loss limit stops the same share of
trades as in the earlier studies; R per trade does not depend on it.

Background (searched 2026-11-18): the definitions are trading-education conventions, not a
standard. The one independent multi-asset test found no reliable edge in order blocks or fair
value gaps, and sweep trades with a 72% win rate still trailed buy-and-hold
(indicatoredge.io/smart-money); vendor claims of 70-97% win rates are unaudited.

Result on 2026-11-18 (H1, six symbols, all day, 2017-2022 / 2023-2026), average R per entry:
  joins at an opposite zone          -0.15 / -0.13   against -0.03 / -0.06 without
  joins after a sweep against (2 bars) -0.08 / -0.09 against -0.03 / -0.06 without
    (same-bar reclaim only: -0.08 / -0.05, no consistent difference; 3 bars: -0.06 / -0.14)
  signals on time at an opposite zone -0.07 / -0.10  against -0.04 / -0.07
  signals on time at an opposite gap  -0.12 / -0.10  against -0.02 / -0.06
  signals on time after a sweep        +0.05 / -0.12 against -0.05 / -0.06 (mixed)
  joins at an opposite gap             -0.09 / -0.07 against -0.03 / -0.07 (mixed)
As entries: sweeps -0.07 to -0.13, fair value gaps -0.11 to -0.18, zones -0.04 to -0.10.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import cli, smc, strategy
from tradebot.backtest import prepare_features
from tradebot.config import STRATEGIES, load_config
from tradebot.data import load_dataset, load_trend_bars

DELAYS = (0, 1, 2, 3, 4, 5)
PERIODS = (("to 2022", ["--to", "2022-12-31"]), ("2023+", ["--from", "2023-01-01"]))


def _period(t: pd.Timestamp) -> str:
    return "2023+" if t.year >= 2023 else "to 2022"


def _outcome(high, low, close, atr, j, side, sl_atr, tp_atr, cost) -> float | None:
    if j >= len(close) - 1 or not np.isfinite(atr[j]):
        return None
    sl, tp = sl_atr * atr[j], tp_atr * atr[j]
    entry = close[j] + side * cost
    stop, target = entry - side * sl, entry + side * tp
    for m in range(j + 1, min(j + 400, len(close))):
        if (low[m] <= stop) if side > 0 else (high[m] >= stop):
            return -1.0
        if (high[m] >= target) if side > 0 else (low[m] <= target):
            return tp / sl
    return None


def cautions(config_path: str, data_dir: Path) -> pd.DataFrame:
    base = load_config(config_path)
    data, specs = load_dataset(data_dir, list(base.symbols), base.timeframe)
    trend_bars = load_trend_bars(data_dir, base)
    scans: dict[str, smc.Scan] = {}
    rows = []
    for setup in STRATEGIES:
        cfg = base.with_setup(setup)
        for symbol, f in prepare_features(cfg, data, trend_bars).items():
            if symbol not in scans:
                scans[symbol] = smc.scan(f)
            found, risk = scans[symbol], cfg.for_symbol(symbol).risk
            high, low, close, atr = (f[k].to_numpy(float) for k in ("high", "low", "close", "atr"))
            cost = specs[symbol].point * 10
            for i in np.flatnonzero(f["signal"].to_numpy() != 0):
                closed = (f["time"].iat[i] + pd.Timedelta(minutes=cfg.bar_minutes)).to_pydatetime()
                if cfg.schedule.closed(closed):
                    continue
                side = int(f["signal"].iat[i])
                for k in DELAYS:
                    j = i + k
                    r = _outcome(high, low, close, atr, j, side, risk.sl_atr, risk.tp_atr, cost)
                    if r is None:
                        continue
                    flags = smc.against(found, j, side)
                    rows.append({"setup": setup, "symbol": symbol, "entry": "on time" if k == 0 else "join 1-5",
                                 "period": _period(f["time"].iat[i]), "r": r, "sweep": "sweep" in flags,
                                 "zone": "zone" in flags, "gap": "gap" in flags, "any": bool(flags)})  # fmt: skip
    return pd.DataFrame(rows)


ENTRIES = ("sweep", "fvg", "zone")


def entries(config_path: str) -> pd.DataFrame:
    original, current, cache = strategy._trigger, {}, {}

    def trigger(df: pd.DataFrame, p) -> pd.Series:
        if p.name != "pullback" or not current:
            return original(df, p)
        key = (len(df), float(df["close"].iat[-1]), float(df["close"].iat[0]))
        if key not in cache:
            found = smc.scan(df)
            cache[key] = {"sweep": found.sweep_side, "fvg": found.fvg_trigger, "zone": found.zone_trigger}
        return pd.Series(cache[key][current["entry"]], index=df.index).astype(int)

    strategy._trigger = trigger
    measured = json.loads(Path(config_path).read_text())
    measured["risk"].update(risk_percent=0.25, max_total_risk_percent=1.0)
    loose = json.loads(json.dumps(measured))
    loose["strategy"].update(use_trend_filter=False, use_adx=False, use_sr_filter=False)
    rows = []
    try:
        with tempfile.TemporaryDirectory() as folder:
            paths = {}
            for name, values in (("none", loose), ("setup filters", measured)):
                paths[name] = Path(folder) / f"{name.replace(' ', '_')}.json"
                paths[name].write_text(json.dumps(values))
            for name in ENTRIES:
                current["entry"] = name
                for filters, path in paths.items():
                    for period, window in PERIODS:
                        out = Path(folder) / f"{name}_{filters[:4]}_{period[:2]}"
                        with contextlib.redirect_stdout(io.StringIO()):
                            cli.main(
                                [
                                    "-c",
                                    str(path),
                                    "backtest",
                                    "--strategy",
                                    "pullback",
                                    *window,
                                    "-o",
                                    str(out),
                                ]
                            )
                        trades = pd.read_csv(next(out.glob("trades_*.csv")))
                        rows.append({"entry": name, "filters": filters, "period": period, "trades": len(trades),
                                     "win_%": 100 * (trades.r_multiple > 0).mean(),
                                     "avg_r": trades.r_multiple.mean()})  # fmt: skip
    finally:
        strategy._trigger = original
    return pd.DataFrame(rows)


def _split(frame: pd.DataFrame, flag: str) -> pd.DataFrame:
    table = frame.groupby(["entry", flag, "period"]).r.agg(["count", "mean"]).round(3).unstack("period")
    table.index = table.index.set_names(["entry", f"{flag} against"])
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--reclaim", type=int, default=smc.SWEEP_RECLAIM_BARS,
                        help="bars a sweep's reclaim may take (1 = the same bar)")  # fmt: skip
    parser.add_argument("--part", type=int, choices=(1, 2), help="run only this part")
    args = parser.parse_args()
    smc.SWEEP_RECLAIM_BARS = args.reclaim
    pd.set_option("display.width", 200)
    if args.part == 2:
        print(entries(args.config).pivot_table(index=["entry", "filters"], columns="period",
                                               values=["trades", "win_%", "avg_r"]).round(3).to_string())  # fmt: skip
        return
    marked = cautions(args.config, args.data)
    print("Part 1: the setups' signals, on time and joined late, split by the cautions on the entry bar")
    print("(count of entries and average R; the four setups together)\n")
    for flag in ("sweep", "zone", "gap", "any"):
        print(_split(marked, flag).to_string(), "\n")
    print("By setup, any caution against (average R):")
    print(marked.groupby(["setup", "any", "period"]).r.mean().round(3).unstack("period").to_string(), "\n")
    if args.part == 1:
        return
    print("Part 2: the concepts as entries, through the engine")
    table = entries(args.config)
    print(table.pivot_table(index=["entry", "filters"], columns="period", values=["trades", "win_%", "avg_r"])
          .round(3).to_string())  # fmt: skip


if __name__ == "__main__":
    main()
