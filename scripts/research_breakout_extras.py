"""Volume confirmation, measured-move targets and a less correlated set of instruments.

    python scripts/research_breakout_extras.py H1,M15              # needs data/ from `tradebot fetch`,
    python scripts/research_breakout_extras.py M30,M5 breakout     # including EURGBP, AUDNZD and US500

Declared before running. One run each per timeframe through the bot's own engine, settings
otherwise default, nothing tuned:

  base         the breakout as it is
  volume       the break bar's tick volume is at least 1.5 times the average of the previous 20 bars
  measured     target = the height of the 20-bar range that broke, projected from the broken level,
               instead of 3 ATR; a trade whose target is under 1.5 times its risk is skipped
  both         volume and measured together

  diversified  the four setups, unchanged, on six instruments chosen before any data was seen:
               EURUSD, USDJPY, XAUUSD (kept) + EURGBP, AUDNZD, US500 (no US dollar leg, or an equity
               index), against the current six, which all have a US dollar leg. Costs for the new
               ones: the same spread floor as the others, and the same commission as a share of the
               position's value ($7 per $100,000).

Adoption rule, also declared first: a breakout variant becomes the default only if it improves the
average trade on BOTH H1 and M15 and then on M30 and M5, which it has not seen. The instrument
list is the user's choice; it is reported, not switched.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from tradebot.backtest import run_backtest
from tradebot.config import load_config
from tradebot.data import load_bars, load_dataset, load_spec, load_trend_bars

base = load_config("config/config.json")
CURRENT = base.symbols
DIVERSIFIED = ("EURUSD", "USDJPY", "XAUUSD", "EURGBP", "AUDNZD", "US500")
SETUPS = ("ema_cross", "breakout", "pullback", "retest")


def report(tf, label, name, cfg, data, specs, htf, per_symbol=False):
    res = run_backtest(cfg, data, specs, htf=htf)
    m = res.metrics
    weeks = (res.end - res.start) / pd.Timedelta(weeks=1)
    r = pd.Series([t.r_multiple for t in res.trades])
    tstat = r.mean() / (r.std(ddof=1) / len(r) ** 0.5) if len(r) > 2 else float("nan")
    print(
        f"{tf:4} {label:12} {name:10} trades {m.trades:5}  {m.trades / weeks:5.1f}/wk  win {m.win_rate:5.1f}%  "
        f"avgR {m.avg_r:+.3f}  PF {m.profit_factor:4.2f}  ret {m.return_pct:+6.1f}%  maxDD {m.max_drawdown_pct:5.1f}%  "
        f"t {tstat:+.2f}  skips {dict(res.skips).get('reward', 0)} reward",
        flush=True,
    )
    if per_symbol:
        print(
            "       per symbol: "
            + "  ".join(
                f"{s} {row['avg_r']:+.2f} ({int(row['trades'])})" for s, row in res.per_symbol.iterrows()
            ),
            flush=True,
        )


def diversified_config(tf):
    spec = load_spec(Path("data"), "US500")
    price = float(load_bars("data/US500_H1.csv")["close"].iloc[-1])
    commission = round(7.0 * spec.contract_size * price / 100_000, 4)
    cfg = replace(
        base,
        timeframe=tf,
        symbols=DIVERSIFIED,
        overrides={
            "XAUUSD": base.overrides["XAUUSD"],
            "USDJPY": base.overrides["USDJPY"],
            "US500": {"backtest": {"commission_per_lot": commission}},
        },
    )
    cfg.validate()
    return cfg, commission


def correlation(symbols):
    closes = pd.DataFrame({s: load_bars(f"data/{s}_D1.csv").set_index("time")["close"] for s in symbols})
    corr = closes.pct_change().dropna().corr().abs()
    pairs = [corr.iloc[i, j] for i in range(len(symbols)) for j in range(i + 1, len(symbols))]
    return sum(pairs) / len(pairs)


parts = sys.argv[2].split(",") if len(sys.argv) > 2 else ["breakout", "diversified"]
for tf in sys.argv[1].split(","):
    cfg_tf = replace(base, timeframe=tf)
    if "breakout" in parts:
        data, specs = load_dataset("data", list(CURRENT), tf)
        htf = load_trend_bars("data", cfg_tf)
        coverage = min(d["tick_volume"].notna().mean() for d in data.values())
        print(f"{tf}: tick volume present on at least {coverage:.1%} of each instrument's bars", flush=True)
        measured = replace(
            cfg_tf.with_strategy(name="breakout", breakout_target="measured"),
            risk=replace(cfg_tf.risk, min_reward_risk=1.5),
        )
        for label, cfg in (
            ("base", cfg_tf.with_strategy(name="breakout")),
            ("volume", cfg_tf.with_strategy(name="breakout", use_volume_filter=True)),
            ("measured", measured),
            ("both", measured.with_strategy(use_volume_filter=True)),
        ):
            report(tf, label, "breakout", cfg, data, specs, htf)
    if "diversified" in parts:
        cfg_div, commission = diversified_config(tf)
        print(
            f"{tf}: mean |correlation| of daily returns: current six {correlation(CURRENT):.2f}, "
            f"diversified six {correlation(DIVERSIFIED):.2f}; US500 commission {commission} per lot",
            flush=True,
        )
        data_c, specs_c = load_dataset("data", list(CURRENT), tf)
        htf_c = load_trend_bars("data", cfg_tf)
        data_d, specs_d = load_dataset("data", list(DIVERSIFIED), tf)
        htf_d = load_trend_bars("data", cfg_div)
        for name in SETUPS:
            report(tf, "current", name, cfg_tf.with_strategy(name=name), data_c, specs_c, htf_c)
            report(
                tf,
                "diversified",
                name,
                cfg_div.with_strategy(name=name),
                data_d,
                specs_d,
                htf_d,
                per_symbol=True,
            )
