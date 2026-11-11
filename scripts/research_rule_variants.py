"""Rule variations from the strategy notes, each measured once through the bot's own engine.

    python scripts/research_rule_variants.py H1,M15            # needs data/ from `tradebot fetch`
    python scripts/research_rule_variants.py M30,M5 base,failexit

Declared before running. One run each per timeframe, default settings otherwise, nothing tuned:

  base        the four setups as they are
  vol         volatility filter on each setup: enter only when ATR(14) is above its 100-bar average
  squeeze     breakout only after a squeeze: Bollinger (20, 2) bandwidth on the bar before the break
              below its 100-bar median
  failexit    breakout: close the position when the break fails (a bar closes back through the
              broken level by 0.25 ATR within 12 bars)
  donchian    breakout, Turtle exits: stop 2.5 ATR, no target, no breakeven/partial/trail; exit when
              a bar closes beyond the opposite 10-bar extreme
  scaleout    breakout and ema_cross: stop 2.5 ATR, no fixed target, close half at +2R, trail the
              rest 2.5 ATR behind price from +2R (breakeven at +1R as now)
  rsi2        mean reversion in a trend: buy when RSI(2) drops below 10 with the close above the
              200-bar average (sell above 90, below it); exit when a bar closes beyond the 5-bar
              average, or after 10 bars; hard stop 2.5 ATR; no other filters

Adoption rule, also declared first: a variant is a candidate only if it improves the average trade
on BOTH H1 and M15; candidates are then run on M30 and M5, which they have not seen, and adopted
only if they improve there too.
"""

import sys
from dataclasses import replace

import pandas as pd

from tradebot import backtest, strategy
from tradebot.backtest import run_backtest
from tradebot.config import load_config
from tradebot.data import load_dataset, load_trend_bars
from tradebot.engine import Engine
from tradebot.indicators import rsi

base = load_config("config/config.json")
MODE = "base"
ORIG_COMPUTE = strategy.compute_signals
ORIG_TRIGGER = strategy._trigger
ORIG_MANAGE = Engine._manage
OWN_FILTERS = dict(strategy.FILTERS)


def trigger(df, p):
    if MODE != "rsi2":
        return ORIG_TRIGGER(df, p)
    close = df["close"]
    fast, average = rsi(close, 2), close.rolling(200).mean()
    up = (fast < 10) & (fast.shift(1) >= 10) & (close > average)
    down = (fast > 90) & (fast.shift(1) <= 90) & (close < average)
    return up.astype(int) - down.astype(int)


def compute(bars, p, timeframe, htf=None):
    df = ORIG_COMPUTE(bars, p, timeframe, htf)
    close = df["close"]
    df["exit_long"], df["exit_short"] = False, False
    if MODE == "vol":
        ok = df["atr"] > df["atr"].rolling(100).mean()
        df["signal"] = df["signal"].where(ok, 0)
    elif MODE == "squeeze":
        width = (4 * close.rolling(20).std(ddof=0) / close.rolling(20).mean()).shift(1)
        df["signal"] = df["signal"].where(width < width.rolling(100).median(), 0)
    elif MODE == "failexit":
        df["exit_long"], df["exit_short"] = df["failed_side"] > 0, df["failed_side"] < 0
    elif MODE == "donchian":
        df["exit_long"] = close < df["low"].rolling(10).min().shift(1)
        df["exit_short"] = close > df["high"].rolling(10).max().shift(1)
    elif MODE == "rsi2":
        average = close.rolling(5).mean()
        df["exit_long"], df["exit_short"] = close > average, close < average
    return df


def manage(self, symbol, spec, params, row, spread, now):
    for position in [p for p in self.broker.positions(self.config.magic) if p.symbol == symbol]:
        if (row.get("exit_long") and position.side > 0) or (row.get("exit_short") and position.side < 0):
            self._close(position, None, now, "exit rule")
    ORIG_MANAGE(self, symbol, spec, params, row, spread, now)


strategy._trigger = trigger
backtest.compute_signals = compute
backtest._ROW_COLUMNS += ["exit_long", "exit_short"]
Engine._manage = manage


def exits(cfg, kind):
    if kind == "donchian":
        return replace(
            cfg,
            risk=replace(cfg.risk, sl_atr=2.5, tp_atr=100.0),
            management=replace(cfg.management, partial_tp=False, breakeven_r=1000.0, trail_start_r=1000.0),
        )
    if kind == "scaleout":
        return replace(
            cfg,
            risk=replace(cfg.risk, sl_atr=2.5, tp_atr=100.0),
            management=replace(cfg.management, partial_tp_r=2.0, trail_start_r=2.0, trail_atr=2.5),
        )
    if kind == "rsi2":
        return replace(
            cfg,
            risk=replace(cfg.risk, sl_atr=2.5, tp_atr=100.0),
            management=replace(
                cfg.management, partial_tp=False, breakeven_r=1000.0, trail_start_r=1000.0, max_hold_bars=10
            ),
        )
    return cfg


RUNS = [
    ("base", "ema_cross"), ("base", "breakout"), ("base", "pullback"), ("base", "retest"),
    ("vol", "ema_cross"), ("vol", "breakout"), ("vol", "pullback"), ("vol", "retest"),
    ("squeeze", "breakout"), ("failexit", "breakout"), ("donchian", "breakout"),
    ("scaleout", "breakout"), ("scaleout", "ema_cross"), ("rsi2", "pullback"),
]  # fmt: skip
only = sys.argv[2].split(",") if len(sys.argv) > 2 else None

for tf in sys.argv[1].split(",") if len(sys.argv) > 1 else ["H1", "M15"]:
    cfg_tf = replace(base, timeframe=tf)
    data, specs = load_dataset("data", list(cfg_tf.symbols), tf)
    htf = load_trend_bars("data", cfg_tf)
    for mode, name in RUNS:
        if only and mode not in only:
            continue
        MODE = mode
        strategy.FILTERS["pullback"] = () if mode == "rsi2" else OWN_FILTERS["pullback"]
        cfg = exits(cfg_tf.with_strategy(name=name), mode)
        res = run_backtest(cfg, data, specs, htf=htf)
        m = res.metrics
        weeks = (res.end - res.start) / pd.Timedelta(weeks=1)
        r = pd.Series([t.r_multiple for t in res.trades])
        tstat = r.mean() / (r.std(ddof=1) / len(r) ** 0.5) if len(r) > 2 else float("nan")
        label = "rsi2" if mode == "rsi2" else name
        print(
            f"{tf:4} {mode:9} {label:10} trades {m.trades:5}  {m.trades / weeks:5.1f}/wk  win {m.win_rate:5.1f}%  "
            f"avgR {m.avg_r:+.3f}  PF {m.profit_factor:4.2f}  ret {m.return_pct:+6.1f}%  maxDD {m.max_drawdown_pct:5.1f}%  "
            f"t {tstat:+.2f}  held {m.avg_bars_held:.0f} bars",
            flush=True,
        )
    strategy.FILTERS["pullback"] = OWN_FILTERS["pullback"]
