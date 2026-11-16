"""Replay fixed signal-quality rules against the same engine with those rules disabled.

    python scripts/research_signal_quality.py
    python scripts/research_signal_quality.py --timeframe M15 --days 365 --setups breakout retest

Uses saved CSV history only. No fitting, terminal connection, downloads or orders. This is a
descriptive comparison on previously available history, not an untouched holdout or evidence
that a rejected signal was certain to lose. Both variants retain the configured costs and risk
limits, and the existing opposite-signal exit behavior.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pandas as pd

from tradebot.backtest import prepare_features, run_backtest
from tradebot.config import STRATEGIES, TIMEFRAME_MINUTES, BotConfig, load_config
from tradebot.data import load_dataset, load_trend_bars


def variant(config: BotConfig, setup: str, enabled: bool) -> BotConfig:
    overrides = deepcopy(config.overrides)
    for parts in overrides.values():
        if "strategy" in parts:
            parts["strategy"].update(name=setup, use_signal_quality_filter=enabled)
    result = replace(config.with_strategy(name=setup, use_signal_quality_filter=enabled), overrides=overrides)
    result.validate()
    return result


def finite_json(value):
    """JSON null represents an undefined metric such as profit factor without losing trades."""
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [finite_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--timeframe", choices=TIMEFRAME_MINUTES, default="H1")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--setups", choices=STRATEGIES, nargs="+", default=list(STRATEGIES))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")

    config = replace(load_config(args.config), timeframe=args.timeframe)
    config.validate()
    data, specs = load_dataset(args.data, list(config.symbols), args.timeframe)
    htf = load_trend_bars(args.data, config)
    if any(frame.empty for frame in data.values()):
        parser.error("each configured symbol needs saved bars")
    # Stop at the most recent bar available for every symbol, so both portfolios share coverage.
    end = min(frame["time"].iloc[-1] for frame in data.values()) + pd.Timedelta(minutes=config.bar_minutes)
    start = end - pd.Timedelta(days=args.days)
    if any(frame["time"].iloc[0] >= start for frame in data.values()):
        parser.error("requested period lacks earlier warmup history for at least one symbol")
    document = {
        "timeframe": args.timeframe,
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "data_directory": str(args.data.resolve()),
        "config": config.to_dict(),
        "limitations": (
            "Fixed rules; no tuning. Previously available history, not an untouched holdout. "
            "Blocked signals are risk-condition flags, not known future losses. Metrics compare "
            "simulated portfolios with the same costs and risk limits. Fewer trades alone can "
            "reduce total loss and drawdown without improving the quality of each trade."
        ),
        "results": [],
    }
    print(f"{args.timeframe}: {start} through {end} (exclusive), {', '.join(config.symbols)}", flush=True)
    print("Fixed thresholds; descriptive replay of existing history, not a fresh holdout.", flush=True)
    for setup in args.setups:
        baseline = variant(config, setup, False)
        filtered = variant(config, setup, True)
        base_features = prepare_features(baseline, data, htf)
        quality_features = prepare_features(filtered, data, htf)
        blocked: Counter[str] = Counter()
        base_signals = 0
        for symbol, frame in base_features.items():
            quality = quality_features[symbol]
            pd.testing.assert_series_equal(frame["signal"], quality["exit_signal"], check_names=False)
            pd.testing.assert_series_equal(frame["raw_signal"], quality["raw_signal"])
            window = (frame["time"] >= start) & (frame["time"] < end)
            candidate = window & frame["signal"].ne(0)
            base_signals += int(candidate.sum())
            rejected = candidate & quality["signal"].eq(0)
            blocked.update(quality.loc[rejected, "block"].value_counts().to_dict())
        comparison = {
            "setup": setup,
            "baseline_signals": base_signals,
            "quality_blocked_signals": sum(blocked.values()),
            "quality_block_reasons": dict(sorted(blocked.items())),
            "variants": {},
        }
        for label, cfg, features in (
            ("baseline", baseline, base_features),
            ("quality", filtered, quality_features),
        ):
            result = run_backtest(cfg, data, specs, start=start, end=end, features=features)
            m = result.metrics
            comparison["variants"][label] = {
                "metrics": m.to_dict(),
                "signals": result.signals,
                "guard_skips": result.skips,
                "per_symbol": result.per_symbol.to_dict(orient="index"),
            }
            print(
                f"{setup:10} {label:8} trades {m.trades:5}  win {m.win_rate:5.1f}%  "
                f"avgR {m.avg_r:+.3f}  PF {m.profit_factor:5.2f}  "
                f"return {m.return_pct:+7.2f}%  maxDD {m.max_drawdown_pct:6.2f}%",
                flush=True,
            )
        print(f"  Quality blocks: {sum(blocked.values())}/{base_signals}; {dict(blocked)}", flush=True)
        document["results"].append(comparison)
    output = args.out or Path("reports") / f"signal-quality-{args.timeframe}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(finite_json(document), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Saved {output}", flush=True)


if __name__ == "__main__":
    main()
