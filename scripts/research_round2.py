"""Research round 2: three different kinds of idea, each fixed in advance and tested once on the holdout.

    pip install -e ".[research]"
    python scripts/research_round2.py

A. fade   (H1)  mean reversion: trade against a stretched move (far from EMA 20, RSI extreme).
B. rates  (D1)  different information: 3-month interbank rate differentials from FRED, traded
                monthly in the direction the differential points or is moving.
C. ml     (H1)  gradient boosting on the bot's own indicators, predicting which of +1.5 ATR or
                -1.5 ATR is reached first within 24 bars.

Each idea is tuned by walk-forward on data before HOLDOUT_START only, then its final parameters
are run once on the holdout through the real engine and simulated broker. Results: README.
"""

from __future__ import annotations

import itertools
import json
import sys
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from research_families import DATA, HOLDOUT_START, family_config

from tradebot.backtest import prepare_features, run_backtest
from tradebot.config import load_config
from tradebot.data import load_bars, load_spec
from tradebot.indicators import ema
from tradebot.optimize import split_windows
from tradebot.strategy import resample

OUT = Path("reports") / "research_round2.json"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=IR3TIB01{}M156N&cosd=2015-01-01"
CURRENCIES = {"US": "US", "EZ": "EZ", "GB": "GB", "JP": "JP", "AU": "AU", "CA": "CA"}
PAIR = {  # (base, quote) as rate areas; gold has no yield of its own
    "EURUSD": ("EZ", "US"), "GBPUSD": ("GB", "US"), "USDJPY": ("US", "JP"),
    "AUDUSD": ("AU", "US"), "USDCAD": ("US", "CA"), "XAUUSD": (None, "US"),
}  # fmt: skip
GRIDS = {
    "fade": {"k": [2.0, 2.5, 3.0], "adx_max": [20.0, 30.0, 100.0], "tp_atr": [1.0, 1.5],
             "session": ["london_new_york", "asia"]},
    "rates": {"mode": ["level", "change1", "change3", "change6"], "sl_atr": [2.0, 3.0]},
}  # fmt: skip
MIN_TRADES = {"fade": 20, "rates": 8}
ML_BARRIER, ML_HORIZON = 1.5, 24
ML_THRESHOLDS = (0.53, 0.55, 0.58, 0.60)

_W: dict = {}


# --- shared data (loaded once per process) --------------------------------------------------------


def load_rates() -> pd.DataFrame:
    """Monthly 3-month interbank rates (OECD via FRED), cached under data/rates, forward-filled."""
    folder = DATA / "rates"
    folder.mkdir(exist_ok=True)
    columns = {}
    for area in CURRENCIES:
        path = folder / f"{area}.csv"
        if not path.exists():
            with urllib.request.urlopen(FRED.format(area), timeout=30) as response:
                path.write_bytes(response.read())
        raw = pd.read_csv(path)
        columns[area] = pd.Series(
            raw.iloc[:, 1].to_numpy(dtype=float), index=pd.PeriodIndex(raw.iloc[:, 0], freq="M")
        )
    months = pd.period_range("2015-01", pd.Timestamp.now().to_period("M"), freq="M")
    return pd.DataFrame(columns).reindex(months).ffill()


def init() -> None:
    if _W:
        return
    base = load_config("config/config.json")
    # the rules these runs used: a single H4 trend filter, and any close through a level is a break
    base = base.with_strategy(trend_timeframes=("H4",), breakout_min_atr=0.0)
    h1 = {s: load_bars(DATA / f"{s}_H1.csv") for s in base.symbols}
    h4 = {(s, "H4"): load_bars(DATA / f"{s}_H4.csv") for s in base.symbols}
    d1 = {s: resample(h4[(s, "H4")], 1440).assign(spread=0) for s in base.symbols}
    d1_cfg, _ = family_config(base, "ema_cross", "D1", "run")
    _W.update(
        base=base,
        specs={s: load_spec(DATA, s) for s in base.symbols},
        ind_h1=prepare_features(base, h1, h4),
        ind_d1=prepare_features(d1_cfg, d1),
        d1_cfg=d1_cfg,
        rates=load_rates(),
    )


def with_signals(frames: dict[str, pd.DataFrame], signal_of) -> dict[str, pd.DataFrame]:
    out = {}
    for symbol, df in frames.items():
        signal = signal_of(symbol, df).where(df["atr"].notna(), 0).fillna(0).astype("int8")
        out[symbol] = df.assign(raw_signal=signal, signal=signal, block="")
    return out


def no_management(cfg, max_hold_bars: int):
    return replace(
        cfg,
        management=replace(
            cfg.management, partial_tp=False, breakeven_r=1000.0, trail_start_r=1000.0,
            close_on_reverse=False, max_hold_bars=max_hold_bars,
        ),
    )  # fmt: skip


# --- A. fade --------------------------------------------------------------------------------------


def fade_setup(p: dict):
    base = _W["base"]
    cfg = no_management(base, max_hold_bars=24)
    cfg = replace(
        cfg,
        risk=replace(cfg.risk, sl_atr=1.5, tp_atr=p["tp_atr"]),
        schedule=replace(cfg.schedule, session=p["session"]),
    )

    def signal(symbol: str, df: pd.DataFrame) -> pd.Series:
        z = (df["close"] - ema(df["close"], 20)) / df["atr"]
        calm = df["adx"] <= p["adx_max"]
        buy = (z <= -p["k"]) & (df["rsi"] <= 30) & calm
        sell = (z >= p["k"]) & (df["rsi"] >= 70) & calm
        state = buy.astype(int) - sell.astype(int)
        return state.where(state != state.shift(1), 0)  # first bar of each stretch only

    return cfg, with_signals(_W["ind_h1"], signal)


# --- B. rates -------------------------------------------------------------------------------------


def rates_setup(p: dict):
    cfg = _W["d1_cfg"]
    cfg = replace(
        cfg,
        risk=replace(cfg.risk, sl_atr=p["sl_atr"], max_total_positions=6, max_total_risk_percent=2.0),
        management=replace(cfg.management, trail_atr=4.5, close_on_reverse=True),
    )
    rates = _W["rates"]

    def signal(symbol: str, df: pd.DataFrame) -> pd.Series:
        base_area, quote_area = PAIR[symbol]
        differential = (rates[base_area] if base_area else 0.0) - rates[quote_area]
        value = (
            differential if p["mode"] == "level" else differential - differential.shift(int(p["mode"][6:]))
        )
        # A daily bar opening at 21:00/22:00 UTC belongs to the next calendar day.
        month = (df["time"] + pd.Timedelta(hours=4)).dt.to_period("M")
        known = (month - 1).map(value)  # last month's average is known when a new month starts
        rebalance = month != month.shift(1)
        return np.sign(known).where(rebalance & (known.abs() >= 0.05), 0)

    return cfg, with_signals(_W["ind_d1"], signal)


SETUPS = {"fade": fade_setup, "rates": rates_setup}


# --- walk-forward over a family's grid ------------------------------------------------------------


def windows_for(family: str):
    start = pd.Timestamp("2017-04-01") if family != "rates" else pd.Timestamp("2017-01-01")
    return split_windows(start.to_pydatetime(), HOLDOUT_START.to_pydatetime(), 4)


def run(cfg, features, start, end=None) -> dict | None:
    try:
        result = run_backtest(cfg, {}, _W["specs"], start=start, end=end, features=features)
    except ValueError:
        return None
    m = result.metrics
    return {
        "trades": m.trades, "sharpe": m.sharpe, "return_pct": m.return_pct, "pf": m.profit_factor,
        "win_rate": m.win_rate, "max_dd": m.max_drawdown_pct, "r": [t.r_multiple for t in result.trades],
    }  # fmt: skip


def evaluate(task: tuple[str, dict]) -> list[dict | None]:
    family, params = task
    init()
    cfg, features = SETUPS[family](params)
    return [run(cfg, features, a, b) for a, b in windows_for(family)]


def stats(r: list[float]) -> dict:
    x = np.array(r, dtype=float)
    if len(x) < 3 or x.std() == 0:
        return {"trades": len(x), "avg_r": float(x.mean()) if len(x) else 0.0, "t": 0.0, "win_rate": 0.0}
    return {
        "trades": len(x),
        "avg_r": float(x.mean()),
        "t": float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))),
        "win_rate": float((x > 0).mean() * 100),
    }


def walk_family(family: str, pool) -> dict:
    grid = GRIDS[family]
    combos = [dict(zip(grid, values, strict=True)) for values in itertools.product(*grid.values())]
    tasks = [(family, c) for c in combos]
    table = list(pool.map(evaluate, tasks)) if pool else [evaluate(t) for t in tasks]

    def score(i: int, k: int) -> float:
        w = table[i][k]
        return (
            w["sharpe"] if w and w["trades"] >= MIN_TRADES[family] and np.isfinite(w["sharpe"]) else -np.inf
        )

    oos, fold_returns, chosen = [], [], []
    for k in range(4):
        best = max(range(len(combos)), key=lambda i: score(i, k))
        nxt = table[best][k + 1]
        chosen.append(combos[best])
        fold_returns.append(nxt["return_pct"] if nxt else 0.0)
        oos += nxt["r"] if nxt else []
    final = combos[max(range(len(combos)), key=lambda i: score(i, 4))]
    init()  # the holdout run happens in this process
    cfg, features = SETUPS[family](final)
    return {
        "combos": len(combos),
        "research": stats(oos) | {"fold_returns": fold_returns, "fold_params": chosen},
        "final_params": final,
        "holdout": holdout_report(run(cfg, features, HOLDOUT_START.to_pydatetime())),
    }


def holdout_report(result: dict | None) -> dict:
    if result is None:
        return {"trades": 0}
    return {k: v for k, v in result.items() if k != "r"} | stats(result["r"])


# --- C. machine learning --------------------------------------------------------------------------


def ml_features(df: pd.DataFrame, symbol_id: int) -> pd.DataFrame:
    a, c = df["atr"], df["close"]
    x = {
        "ema_gap": (df["ema_fast"] - df["ema_slow"]) / a,
        "price_vs_slow": (c - df["ema_slow"]) / a,
        "rsi": df["rsi"],
        "adx": df["adx"],
        "macd": df["macd_hist"] / a,
        "trend": df["trend"],
        "room_up": (df["resistance"] - c) / a,
        "room_down": (c - df["support"]) / a,
        "volatility": (a / c) / (a / c).rolling(200).mean(),
        "bar_range": (df["high"] - df["low"]) / a,
        "hour": df["time"].dt.hour,
        "weekday": df["time"].dt.day_of_week,
        "symbol": symbol_id,
    }
    for n in (1, 4, 12, 24, 72):
        x[f"move_{n}"] = (c - c.shift(n)) / a
    return pd.DataFrame(x)


def ml_label(df: pd.DataFrame) -> np.ndarray:
    """1 if +1.5 ATR is reached before -1.5 ATR within 24 bars, 0 for the reverse, NaN if unclear."""
    c, h, low, a = (df[k].to_numpy(dtype=float) for k in ("close", "high", "low", "atr"))
    up, down = c + ML_BARRIER * a, c - ML_BARRIER * a
    label = np.full(len(df), np.nan)
    done = np.zeros(len(df), dtype=bool)
    for k in range(1, ML_HORIZON + 1):
        future_high = np.concatenate([h[k:], np.full(k, np.nan)])
        future_low = np.concatenate([low[k:], np.full(k, np.nan)])
        hit_up, hit_down = (future_high >= up) & ~done, (future_low <= down) & ~done
        label[hit_up & ~hit_down] = 1.0
        label[hit_down & ~hit_up] = 0.0
        done |= hit_up | hit_down
    return label


def ml_family() -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier

    init()
    frames = _W["ind_h1"]
    parts = []
    for i, (symbol, df) in enumerate(frames.items()):
        part = ml_features(df, i)
        part["y"], part["time"], part["sym"] = ml_label(df), df["time"], symbol
        parts.append(part[df["atr"].notna().to_numpy()])
    table = pd.concat(parts, ignore_index=True)
    columns = [c for c in table.columns if c not in ("y", "time", "sym")]
    table["p"] = np.nan

    def fit_predict(test_start: pd.Timestamp, test_end: pd.Timestamp) -> None:
        embargo = test_start - pd.Timedelta(days=5)  # labels look 24 bars ahead
        train = table[(table["time"] < embargo) & table["y"].notna()]
        test = (table["time"] >= test_start) & (table["time"] < test_end)
        model = HistGradientBoostingClassifier(
            max_depth=3,
            learning_rate=0.05,
            max_iter=200,
            min_samples_leaf=200,
            l2_regularization=1.0,
            random_state=0,
        )
        model.fit(train[columns], train["y"])
        table.loc[test, "p"] = model.predict_proba(table.loc[test, columns])[:, 1]

    edges = [pd.Timestamp(f"{y}-01-01") for y in range(2020, 2025)] + [HOLDOUT_START]
    for a, b in itertools.pairwise(edges):
        fit_predict(a, b)
    fit_predict(HOLDOUT_START, pd.Timestamp("2100-01-01"))

    def accuracy(rows: pd.DataFrame) -> dict:
        rows = rows[rows["y"].notna() & rows["p"].notna()]
        out = {
            "all": {"n": len(rows), "accuracy": float(((rows["p"] > 0.5) == (rows["y"] == 1)).mean() * 100)}
        }
        for t in ML_THRESHOLDS:
            sure = rows[(rows["p"] >= t) | (rows["p"] <= 1 - t)]
            hit = ((sure["p"] > 0.5) == (sure["y"] == 1)).mean() * 100 if len(sure) else float("nan")
            out[f"p>={t}"] = {
                "n": len(sure),
                "share": len(sure) / max(len(rows), 1) * 100,
                "accuracy": float(hit),
            }
        return out

    cfg = no_management(_W["base"], max_hold_bars=ML_HORIZON)
    cfg = replace(cfg, risk=replace(cfg.risk, sl_atr=ML_BARRIER, tp_atr=ML_BARRIER))

    def features_at(threshold: float):
        by_symbol = {s: g.set_index("time")["p"] for s, g in table.groupby("sym")}

        def signal(symbol: str, df: pd.DataFrame) -> pd.Series:
            p = df["time"].map(by_symbol[symbol])
            return (p >= threshold).astype(int) - (p <= 1 - threshold).astype(int)

        return with_signals(frames, signal)

    research_runs = {
        t: run(cfg, features_at(t), edges[0].to_pydatetime(), HOLDOUT_START.to_pydatetime())
        for t in ML_THRESHOLDS
    }
    usable = {t: r for t, r in research_runs.items() if r and r["trades"] >= 100}
    best = max(usable, key=lambda t: np.mean(usable[t]["r"])) if usable else ML_THRESHOLDS[0]
    research, holdout = table[table["time"] < HOLDOUT_START], table[table["time"] >= HOLDOUT_START]
    return {
        "research_accuracy": accuracy(research),
        "holdout_accuracy": accuracy(holdout),
        "research": {str(t): holdout_report(r) for t, r in research_runs.items()},
        "final_params": {"threshold": best},
        "holdout": holdout_report(run(cfg, features_at(best), HOLDOUT_START.to_pydatetime())),
    }


# --- main -----------------------------------------------------------------------------------------


def show(name: str, block: dict) -> None:
    def line(d: dict) -> str:
        if not d.get("trades"):
            return "no trades"
        extra = f" · return {d['return_pct']:+.1f}% · PF {d['pf']:.2f}" if "return_pct" in d else ""
        return f"{d['trades']} trades · winners {d['win_rate']:.1f}% · avg {d['avg_r']:+.3f}R · t {d['t']:+.2f}{extra}"

    print(f"\n== {name}: final parameters {block['final_params']}")
    if "fold_returns" in block.get("research", {}):
        r = block["research"]
        print(f"   research (walk-forward): {line(r)} · folds {[round(x, 1) for x in r['fold_returns']]}")
    else:
        for threshold, r in block["research"].items():
            print(f"   research p>={threshold}: {line(r)}")
        for where in ("research_accuracy", "holdout_accuracy"):
            print(f"   {where}: " + " | ".join(
                f"{k}: {v['accuracy']:.1f}% of {v['n']:,}" for k, v in block[where].items()))  # fmt: skip
    print(f"   HOLDOUT: {line(block['holdout'])}", flush=True)


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    results = {}
    try:
        with ProcessPoolExecutor(initializer=init) as pool:
            for family in ("rates", "fade"):
                results[family] = walk_family(family, pool)
                show(family, results[family])
    except (BrokenProcessPool, OSError) as exc:
        print(f"worker pool failed ({exc}); continuing in one process")
        for family in ("rates", "fade"):
            if family not in results:
                init()
                results[family] = walk_family(family, None)
                show(family, results[family])
    results["ml"] = ml_family()
    show("ml", results["ml"])
    OUT.write_text(json.dumps(results, indent=1, default=float))


if __name__ == "__main__":
    main()
