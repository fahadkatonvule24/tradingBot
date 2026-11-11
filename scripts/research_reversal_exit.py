"""What would obeying the reversal warning have done to the bot's own trades?

    python scripts/research_reversal_exit.py H1,M15        # needs data/ from `tradebot fetch`
    python scripts/research_reversal_exit.py M30,M5        # the confirmation run

The dashboard warns when the market turns against an open position (``tradebot.reversal``). This
script runs the same check, bar by bar, on every trade of a backtest and closes the trade when the
check says so. Declared before running. One run each per timeframe through the bot's own engine,
default settings otherwise, nothing tuned:

  base   the four setups as they are (an opposite signal already closes a position)
  exit   close at the close of the first bar on which the check reaches the "get out" level: the
         opposite signal, or two signs standing together
  warn   close at the close of the first bar on which the check shows any sign at all

The check is a warning for a person, and it ships as one whatever these numbers say. They are
here so the warning comes with what it has been worth. An exit rule would be adopted for the bot
only by the usual rule: better average trade on BOTH H1 and M15, then on M30 and M5.
"""

import sys
from dataclasses import replace

import pandas as pd

from tradebot import backtest
from tradebot.backtest import run_backtest
from tradebot.config import load_config
from tradebot.data import load_dataset, load_trend_bars
from tradebot.engine import Engine
from tradebot.reversal import EXIT, Watch

base = load_config("config/config.json")
MODE = "base"
ORIG_MANAGE = Engine._manage
WATCHES: dict[int, Watch] = {}
PREVIOUS: dict[str, dict] = {}
COUNTS = {"closed": 0}


def manage(self, symbol, spec, params, row, spread, now):
    if MODE != "base":
        for position in [p for p in self.broker.positions(self.config.magic) if p.symbol == symbol]:
            watch = WATCHES.get(position.ticket)
            if watch is None:  # first bar after the entry: the bar before it is the reference
                watch = WATCHES[position.ticket] = Watch(position.side, PREVIOUS.get(symbol))
            watch.update(row)
            if watch.level == EXIT or (MODE == "warn" and watch.level):
                COUNTS["closed"] += 1
                self._close(position, None, now, "reversal check")
    PREVIOUS[symbol] = row
    ORIG_MANAGE(self, symbol, spec, params, row, spread, now)


backtest._ROW_COLUMNS += ["ema_fast", "ema_slow", "range_high", "range_low"]
backtest._OPTIONAL_COLUMNS += ("failed_side", "failed_level")
Engine._manage = manage

for tf in sys.argv[1].split(",") if len(sys.argv) > 1 else ["H1", "M15"]:
    cfg_tf = replace(base, timeframe=tf)
    data, specs = load_dataset("data", list(cfg_tf.symbols), tf)
    htf = load_trend_bars("data", cfg_tf)
    for name in ("ema_cross", "breakout", "pullback", "retest"):
        for mode in ("base", "exit", "warn"):
            MODE = mode
            WATCHES.clear()
            PREVIOUS.clear()
            COUNTS["closed"] = 0
            res = run_backtest(cfg_tf.with_strategy(name=name), data, specs, htf=htf)
            m = res.metrics
            r = pd.Series([t.r_multiple for t in res.trades])
            tstat = r.mean() / (r.std(ddof=1) / len(r) ** 0.5) if len(r) > 2 else float("nan")
            print(
                f"{tf:4} {name:10} {mode:5} trades {m.trades:5}  win {m.win_rate:5.1f}%  avgR {m.avg_r:+.3f}  "
                f"PF {m.profit_factor:4.2f}  ret {m.return_pct:+6.1f}%  maxDD {m.max_drawdown_pct:5.1f}%  "
                f"t {tstat:+.2f}  held {m.avg_bars_held:.0f} bars  closed by the check {COUNTS['closed']}",
                flush=True,
            )
