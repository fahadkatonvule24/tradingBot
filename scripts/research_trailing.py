"""Is moving the stop (breakeven, trailing) worth it? Measured on the bot's own signals.

    python scripts/research_trailing.py [--config config/config.json]

Each setup's signals go through the backtest engine four times, changing only how an open trade
is managed (stops, target, costs, hours and limits as configured, risk 0.25% so the loss limits stop
the same share of trades as in the other studies):

  A fixed           the first stop and the 2R target, nothing moved
  B breakeven       stop to entry + 0.1R once a bar closes 1R in profit
  C breakeven+trail B, and from 1.5R the stop follows the close at 2 ATR (never loosened)
  D all             C, and half the position closed at 1.5R (the bot's own management)

C is what ``management.auto_trailing`` does to trades placed by hand. Stops move on closed bars
here, as the engine moves them.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
from pathlib import Path

import pandas as pd

from tradebot import cli
from tradebot.config import STRATEGIES

OFF = 999.0
VARIANTS = {
    "A fixed": {"breakeven_r": OFF, "trail_start_r": OFF, "partial_tp": False},
    "B breakeven": {"breakeven_r": 1.0, "trail_start_r": OFF, "partial_tp": False},
    "C breakeven+trail": {"breakeven_r": 1.0, "trail_start_r": 1.5, "partial_tp": False},
    "D all": {"breakeven_r": 1.0, "trail_start_r": 1.5, "partial_tp": True},
}
FULL = {"breakeven_r": 1.0, "trail_start_r": 1.5, "partial_tp": True}
REVERSAL = {
    "no reversal exit": {**FULL, "close_on_reverse": False},
    "close on reversal": {**FULL, "close_on_reverse": True},
    "close on reversal in profit": {**FULL, "close_on_reverse": True, "reverse_exit_in_profit_only": True},
}
"""Part 2: closing on an opposite signal, always (the bot's rule) or only while the trade is in
profit, on top of the full management above."""
PERIODS = (("to 2022", ["--to", "2022-12-31"]), ("2023+", ["--from", "2023-01-01"]))


def run(config_path: str, variants: dict | None = None) -> pd.DataFrame:
    base = json.loads(Path(config_path).read_text())
    base["risk"].update(risk_percent=0.25, max_total_risk_percent=1.0)
    rows = []
    with tempfile.TemporaryDirectory() as folder:
        for variant, management in (variants or VARIANTS).items():
            cfg = json.loads(json.dumps(base))
            cfg["management"].update({"close_on_reverse": False, **management})  # only management differs
            path = Path(folder) / "cfg.json"
            path.write_text(json.dumps(cfg))
            for setup in ("configured", *STRATEGIES):
                chosen = [] if setup == "configured" else ["--strategy", setup]
                for period, window in PERIODS:
                    out = Path(folder) / f"{abs(hash(variant))}_{setup}_{period[:2]}"
                    with contextlib.redirect_stdout(io.StringIO()):
                        cli.main(["-c", str(path), "backtest", *chosen, *window, "-o", str(out)])
                    t = pd.read_csv(next(out.glob("trades_*.csv")))
                    rows.append({"variant": variant, "setup": setup, "period": period, "trades": len(t),
                                 "avg_r": t.r_multiple.mean(), "win_%": 100 * (t.r_multiple > 0).mean(),
                                 "worst_run_r": _worst_run(t.r_multiple)})  # fmt: skip
    return pd.DataFrame(rows)


def _worst_run(r: pd.Series) -> float:
    """The deepest fall of the cumulative R curve: the drawdown a trader sits through."""
    curve = r.cumsum()
    return float((curve - curve.cummax()).min()) if len(curve) else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config/config.json")
    args = parser.parse_args()
    pd.set_option("display.width", 220)
    table = pd.concat([run(args.config), run(args.config, REVERSAL)])
    for value in ("avg_r", "win_%", "worst_run_r"):
        print(f"--- {value} ---")
        print(
            table.pivot_table(index="setup", columns=["period", "variant"], values=value)
            .round(3)
            .to_string(),
            "\n",
        )


if __name__ == "__main__":
    main()
