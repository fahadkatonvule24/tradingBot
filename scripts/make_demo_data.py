"""Write synthetic M15 history so the backtester can be tried without a MetaTrader terminal.

    python scripts/make_demo_data.py --out demo-data
    tradebot backtest --data demo-data --symbols EURUSD GBPUSD

The series is a seeded random walk with trending and ranging regimes. It is for trying the
tool and for CI, not for judging a strategy; use `tradebot fetch` for real broker history.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tradebot.data import synthetic_bars

SERIES = {"EURUSD": (1.10, 1), "GBPUSD": (1.27, 2), "USDJPY": (150.0, 3), "AUDUSD": (0.66, 4)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("demo-data"))
    parser.add_argument("--bars", type=int, default=30_000)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for symbol, (price, seed) in SERIES.items():
        bars = synthetic_bars(
            n=args.bars,
            seed=seed,
            price=price,
            volatility=0.0006,
            spread_points=12 if symbol == "USDJPY" else 8,
        )
        bars.to_csv(args.out / f"{symbol}_M15.csv", index=False)
        print(f"wrote {args.out / f'{symbol}_M15.csv'} ({len(bars):,} bars)")


if __name__ == "__main__":
    main()
