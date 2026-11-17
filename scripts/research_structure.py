"""What the chart patterns and structure breaks of ``tradebot.structure`` have been worth as entries.

    python scripts/research_structure.py [--config config/config.json]

Every bar on which ``structure.patterns`` reports a pattern's break (``broke`` is that bar) becomes
an entry trigger on the pattern's side; a break of structure (``structure_state(...)["broken"]``
turning non-zero) is one too. The triggers replace the pullback setup's, so the bot's engine,
stops, costs and trading hours apply, first without the trend filters (the patterns as the books
trade them) and then with them. Swings are rebuilt bar by bar from what was confirmed by then,
exactly as ``structure.swings(features, upto=i)`` returns them (checked on a sample of bars).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import cli, strategy
from tradebot.structure import SWING_BARS, Swing, patterns, structure_state, swings


def incremental_swings(features: pd.DataFrame):
    """Yield (i, swings confirmed by bar i) for every bar, without recomputing from scratch."""
    k, n = SWING_BARS, len(features)
    high, low = features["high"].to_numpy(float), features["low"].to_numpy(float)
    atr = features["atr"].to_numpy(float)
    top = pd.Series(high).rolling(2 * k + 1, center=True).max().to_numpy()
    bottom = pd.Series(low).rolling(2 * k + 1, center=True).min().to_numpy()
    raw: list[Swing] = []
    for i in range(n):
        j = i - k  # bar j becomes a confirmed swing when bar i = j + k closes
        if j >= k:
            is_high, is_low = high[j] == top[j], low[j] == bottom[j]
            if is_high and is_low:
                is_low = bool(raw) and raw[-1].kind == "high"
                is_high = not is_low
            for kind, price, hit in (("high", high[j], is_high), ("low", low[j], is_low)):
                if not hit:
                    continue
                if raw and raw[-1].kind == kind:
                    if (price > raw[-1].price) if kind == "high" else (price < raw[-1].price):
                        raw[-1] = Swing(j, kind, float(price))
                    continue
                raw.append(Swing(j, kind, float(price)))
        yield i, _labelled(raw[-8:], raw, atr)


def _labelled(tail: list[Swing], raw: list[Swing], atr: np.ndarray) -> list[Swing]:
    start = len(raw) - len(tail)
    out = []
    for pos, s in enumerate(tail, start):
        before = next((r for r in reversed(raw[:pos]) if r.kind == s.kind), None)
        label = ""
        if before is not None:
            tolerance = 0.1 * atr[s.i] if np.isfinite(atr[s.i]) else 0.0
            if abs(s.price - before.price) <= tolerance:
                label = "EH" if s.kind == "high" else "EL"
            elif s.price > before.price:
                label = "HH" if s.kind == "high" else "HL"
            else:
                label = "LH" if s.kind == "high" else "LL"
        out.append(Swing(s.i, s.kind, s.price, label))
    return out


FAMILIES = ("double", "head_shoulders", "triangle", "structure_break")


def family(kind: str) -> str:
    return "triangle" if kind.endswith("triangle") else "head_shoulders" if "head" in kind else "double"


def triggers(features: pd.DataFrame, check: bool = False) -> dict[str, pd.Series]:
    out = {name: np.zeros(len(features), dtype=int) for name in FAMILIES}
    close = features["close"].to_numpy(float)
    previous_break = 0
    sample = set(random.Random(1).sample(range(len(features)), min(40, len(features)))) if check else set()
    for i, points in incremental_swings(features):
        if i in sample:  # the incremental build must match the module's own, bar for bar
            expected = swings(features, upto=i)[-8:]
            assert [(s.i, s.kind, s.label) for s in points] == [(s.i, s.kind, s.label) for s in expected], i
        if len(points) < 3:
            continue
        for p in patterns(features, points, upto=i):
            if p["broke"] == i and p["side"]:
                out[family(p["kind"])][i] = p["side"]
        broken = structure_state(points, close[i])["broken"]
        if broken and broken != previous_break:
            out["structure_break"][i] = broken
        previous_break = broken
    return {name: pd.Series(values, index=features.index) for name, values in out.items()}


def run(config_path: str, check: bool) -> pd.DataFrame:
    original, current, cache = strategy._trigger, {}, {}

    def trigger(df: pd.DataFrame, p) -> pd.Series:
        if p.name != "pullback" or not current:
            return original(df, p)
        key = (len(df), float(df["close"].iat[-1]), float(df["close"].iat[0]))
        if key not in cache:
            cache[key] = triggers(df, check)
        return cache[key][current["family"]]

    strategy._trigger = trigger
    base = json.loads(Path(config_path).read_text())
    loose = json.loads(json.dumps(base))
    loose["strategy"].update(use_trend_filter=False, use_adx=False, use_sr_filter=False)
    rows = []
    try:
        with tempfile.TemporaryDirectory() as folder:
            loose_path = Path(folder) / "loose.json"
            loose_path.write_text(json.dumps(loose))
            for name in FAMILIES:
                current["family"] = name
                for filters, path in (("none", str(loose_path)), ("trend", config_path)):
                    for period, window in (
                        ("to 2022", ["--to", "2022-12-31"]),
                        ("2023+", ["--from", "2023-01-01"]),
                    ):
                        out = Path(folder) / f"{name}_{filters}_{period[:2]}"
                        with contextlib.redirect_stdout(io.StringIO()):
                            cli.main(
                                ["-c", path, "backtest", "--strategy", "pullback", *window, "-o", str(out)]
                            )
                        trades = pd.read_csv(next(out.glob("trades_*.csv")))
                        rows.append({"entry": name, "filters": filters, "period": period, "trades": len(trades),
                                     "avg_r": trades.r_multiple.mean()})  # fmt: skip
    finally:
        strategy._trigger = original
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--no-check", action="store_true", help="skip the bar-by-bar consistency check")
    args = parser.parse_args()
    table = run(args.config, not args.no_check)
    pd.set_option("display.width", 200)
    print(
        table.pivot_table(index=["entry", "filters"], columns="period", values=["trades", "avg_r"])
        .round(3)
        .to_string()
    )


if __name__ == "__main__":
    main()
