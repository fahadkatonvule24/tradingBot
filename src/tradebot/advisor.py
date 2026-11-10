"""Trade advice for a person at the screen.

For each symbol the advisor asks the live engine the questions it asks before trading: is there
a signal on the last closed bar, do the filters agree, would a guard block it, and what would the
position be (lot size, stop, target, money at risk)? The plan is priced at the live bid/ask, so
"where" and "how much" are what an order sent right now would get.

Advice is only ever *actionable* when the engine itself would open the trade. Everything else is
context: what the next trigger is, which filters currently pass, and the plan if it fired now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from tradebot.broker.base import SymbolSpec, Tick
from tradebot.config import ScheduleParams, StrategyParams, SymbolParams
from tradebot.engine import Engine, Rejection, stop_distances
from tradebot.indicators import rsi
from tradebot.strategy import BLOCK_LABELS, FILTERS, _near_fib, explain

WORD = {1: "BUY", -1: "SELL"}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class TradePlan:
    side: int
    entry: float
    stop: float
    target: float
    volume: float
    risk_amount: float
    """Account-currency loss if the stop is hit (the spread is already inside the distance)."""
    reward_amount: float
    """Account-currency profit if the target is hit."""
    risk_percent: float
    stop_distance: float
    target_distance: float
    reward_risk: float
    rejected: str = ""
    """Why the engine would not size this trade (minimum lot, portfolio risk, margin)."""


@dataclass(frozen=True)
class Advice:
    symbol: str
    action: str
    """BUY / SELL (take it now), SKIP (signal, but a guard says no), WAIT, CLOSED."""
    headline: str
    detail: str
    side: int
    """Direction the plan and checks refer to; 0 when there is nothing to plan."""
    actionable: bool
    bar_time: datetime
    """Open time (UTC) of the last closed bar the advice is based on."""
    decision_at: datetime | None
    """When the forming bar closes: the next moment the advice can change."""
    trigger: str = ""
    trigger_level: float | None = None
    checks: list[Check] = field(default_factory=list)
    blocker: str | None = None
    plan: TradePlan | None = None
    entries_from: datetime | None = None
    """When trading hours are closed: the first bar close at which an entry is allowed again."""


def next_entry_time(schedule: ScheduleParams, after: datetime, bar: timedelta) -> datetime | None:
    """First bar close at or after ``after`` that the schedule allows (searches one week)."""
    t = after
    for _ in range(int(timedelta(days=7) / bar) + 1):
        if not schedule.closed(t):
            return t
        t += bar
    return None


def recent_signals(
    features: pd.DataFrame, params: SymbolParams, schedule: ScheduleParams, bar_minutes: int, limit: int = 8
) -> dict:
    """The latest signals in the loaded window and what happened next, so a screen full of WAIT
    still shows what the strategy does and how often.

    The outcome is deliberately simple: which was touched first, the stop or the full target
    (the stop, if one bar touches both). It ignores spread, commission and the breakeven, partial
    and trailing rules, so it is a guide to direction, not a P/L statement.
    """
    time_, signal = features["time"], features["signal"].to_numpy()
    high, low, close, atr_ = (features[k].to_numpy(dtype=float) for k in ("high", "low", "close", "atr"))
    bar = pd.Timedelta(minutes=bar_minutes)
    fired = np.flatnonzero(signal != 0)
    tradeable = [i for i in fired if schedule.closed((time_.iloc[i] + bar).to_pydatetime()) is None]
    items = []
    for i in fired[::-1][:limit]:
        side, entry = int(signal[i]), close[i]
        sl, tp = params.risk.sl_atr * atr_[i], params.risk.tp_atr * atr_[i]
        stop, target = entry - side * sl, entry + side * tp
        outcome, r = "open", (close[-1] - entry) * side / sl
        for j in range(i + 1, len(close)):
            if (low[j] <= stop) if side > 0 else (high[j] >= stop):
                outcome, r = "stop", -1.0
                break
            if (high[j] >= target) if side > 0 else (low[j] <= target):
                outcome, r = "target", tp / sl
                break
        closed_at = (time_.iloc[i] + bar).to_pydatetime()
        items.append(
            {"time": closed_at, "side": side, "entry": entry, "stop": stop, "target": target,
             "outcome": outcome, "r": r, "blocked": schedule.closed(closed_at)}
        )  # fmt: skip
    weeks = max((time_.iloc[-1] - time_.iloc[0]) / pd.Timedelta(weeks=1), 1e-9)
    return {"items": items, "tradeable": len(tradeable), "weeks": float(weeks)}


def pip_size(spec: SymbolSpec) -> float:
    """FX quotes with 3 or 5 digits have a fractional pip; everything else is quoted in points."""
    return spec.point * 10 if spec.digits in (3, 5) else spec.point


def advise(
    engine: Engine,
    symbol: str,
    spec: SymbolSpec,
    params: SymbolParams,
    features: pd.DataFrame,
    tick: Tick,
    now: datetime,
) -> Advice:
    config = engine.config
    p = params.strategy
    row = features.iloc[-1]
    bar = timedelta(minutes=config.bar_minutes)
    bar_time = pd.Timestamp(row["time"]).to_pydatetime()
    bar_close = bar_time + bar

    if now - bar_close > bar * config.schedule.max_bar_age_bars:
        return Advice(
            symbol, "CLOSED", "Market closed",
            f"The last {config.timeframe} bar closed at {bar_close:%a %H:%M} UTC; advice resumes with the next bar.",
            0, False, bar_time, None,
        )  # fmt: skip
    if pd.isna(row["atr"]):
        return Advice(symbol, "WAIT", "Not enough history", "Indicators are still warming up.", 0, False,
                      bar_time, bar_close + bar)  # fmt: skip

    signal, raw = int(row["signal"]), int(row["raw_signal"])
    side = signal or raw or watch_side(row, p)
    spread = tick.ask - tick.bid
    blocker = engine.entry_blocker(symbol, spec, params, row, spread, now)
    plan = trade_plan(engine, symbol, spec, params, row, tick, side)
    trigger, level = next_trigger(features, p, side, tick, spec, config.timeframe)
    entries_from = None
    if config.schedule.closed(now):
        entries_from = next_entry_time(config.schedule, bar_close + bar, bar)
    common = {
        "side": side,
        "bar_time": bar_time,
        "decision_at": bar_close + bar,
        "trigger": trigger,
        "trigger_level": level,
        "checks": filter_checks(features, p, side),
        "blocker": blocker,
        "plan": plan,
        "entries_from": entries_from,
    }

    if signal:
        word = WORD[signal]
        why_not = blocker or plan.rejected
        if why_not:
            return Advice(symbol, "SKIP", f"{word} signal, but skip it", why_not, actionable=False, **common)
        return Advice(
            symbol, word, f"{word.title()} {symbol} now",
            f"{explain(row, p)}. Valid until the {config.timeframe} bar closes at {bar_close + bar:%H:%M} UTC.",
            actionable=True, **common,
        )  # fmt: skip
    if raw:
        label = BLOCK_LABELS.get(row["block"], row["block"])
        return Advice(
            symbol, "WAIT", f"{WORD[raw].title()} trigger filtered out", f"The trigger fired but {label}.",
            actionable=False, **common,
        )  # fmt: skip
    return Advice(symbol, "WAIT", "No setup yet", trigger, actionable=False, **common)


def watch_side(row: pd.Series, p: StrategyParams) -> int:
    """The direction worth watching: the higher-timeframe trend when the strategy filters on it."""
    if p.use_trend_filter and "trend" in FILTERS[p.name] and row["trend"] != 0:
        return int(row["trend"])
    if p.name == "ema_cross":
        return -1 if row["ema_fast"] > row["ema_slow"] else 1  # the next cross goes the other way
    return 1 if row["macd_hist"] >= 0 else -1


def trade_plan(
    engine: Engine,
    symbol: str,
    spec: SymbolSpec,
    params: SymbolParams,
    row: pd.Series,
    tick: Tick,
    side: int,
) -> TradePlan:
    """Entry at the live price, stop and target at the engine's distances, sized by the engine."""
    spread = tick.ask - tick.bid
    entry = tick.ask if side > 0 else tick.bid
    sized = engine.plan_entry(symbol, spec, params, row, spread, side)
    if isinstance(sized, Rejection):
        sl, tp = stop_distances(spec, params.risk, float(row["atr"]), spread)
        volume = risk = reward = percent = 0.0
        rejected = sized.message
    else:
        sl, tp, volume, rejected = sized.sl_distance, sized.tp_distance, sized.volume, ""
        risk, reward, percent = sized.risk_amount, sized.reward_amount, sized.risk_percent
    return TradePlan(
        side=side,
        entry=entry,
        stop=spec.round_price(entry - side * sl),
        target=spec.round_price(entry + side * tp),
        volume=volume,
        risk_amount=risk,
        reward_amount=reward,
        risk_percent=percent,
        stop_distance=sl,
        target_distance=tp,
        reward_risk=tp / sl if sl else 0.0,
        rejected=rejected,
    )


def filter_checks(features: pd.DataFrame, p: StrategyParams, side: int) -> list[Check]:
    """The strategy's filters for ``side``, evaluated on the last closed bar as compute_signals does."""
    row = features.iloc[-1]
    applicable = FILTERS[p.name]
    checks = []
    if "trend" in applicable and p.use_trend_filter:
        state = {1: "up", -1: "down"}.get(int(row["trend"]), "flat")
        checks.append(Check(f"{p.trend_timeframe} trend", int(row["trend"]) == side,
                            f"{state} (close vs EMA {p.trend_ema})"))  # fmt: skip
    if "adx" in applicable and p.use_adx:
        checks.append(Check("Trend strength", bool(row["adx"] >= p.adx_min),
                            f"ADX {row['adx']:.1f}, needs ≥ {p.adx_min:g}"))  # fmt: skip
    if "macd" in applicable and p.use_macd:
        hist = float(row["macd_hist"])
        checks.append(Check("Momentum", bool(np.sign(hist) == side),
                            f"MACD histogram {'positive' if hist > 0 else 'negative'}"))  # fmt: skip
    if "rsi" in applicable:
        if side > 0:
            ok, need = row["rsi"] <= p.rsi_buy_max, f"≤ {p.rsi_buy_max:g} to buy"
        else:
            ok, need = row["rsi"] >= p.rsi_sell_min, f"≥ {p.rsi_sell_min:g} to sell"
        checks.append(Check("Not overextended", bool(ok), f"RSI {row['rsi']:.1f}, needs {need}"))
    if "sr" in applicable and p.use_sr_filter:
        level, what = (row["resistance"], "resistance") if side > 0 else (row["support"], "support")
        room = (level - row["close"]) * side / row["atr"]
        checks.append(Check("Room to run", bool(room >= p.sr_min_atr),
                            f"{room:.1f} ATR to {what}, needs ≥ {p.sr_min_atr:g}"))  # fmt: skip
    if "fib" in applicable and p.use_fib_filter:
        near = bool(_near_fib(features, p, pd.Series(side > 0, index=features.index)).iloc[-1])
        checks.append(
            Check("Fibonacci level", near, ("near" if near else "not near") + " a 38/50/62% retracement")
        )
    return checks


def next_trigger(
    features: pd.DataFrame,
    p: StrategyParams,
    side: int,
    tick: Tick,
    spec: SymbolSpec,
    timeframe: str,
) -> tuple[str, float | None]:
    """What has to happen for the strategy to fire on ``side``, and the price level if it has one."""
    row = features.iloc[-1]
    word = "buy" if side > 0 else "sell"
    above = "above" if side > 0 else "below"
    atr_ = float(row["atr"])
    if p.name == "ema_cross":
        gap = (row["ema_fast"] - row["ema_slow"]) / atr_
        if gap * side < 0:
            return (
                f"A {word} triggers when EMA {p.fast_ema} closes {above} EMA {p.slow_ema} "
                f"(now {abs(gap):.2f} ATR {'below' if side > 0 else 'above'} it).",
                None,
            )
        return (
            f"EMA {p.fast_ema} is already {above} EMA {p.slow_ema}; the next {word} needs a fresh cross.",
            None,
        )
    if p.name == "breakout":
        window = features.tail(p.breakout_period)
        level = float(window["high"].max() if side > 0 else window["low"].min())
        price = tick.ask if side > 0 else tick.bid
        away = (level - price) * side
        where = f"{away / atr_:.2f} ATR away" if away > 0 else "price is through it; the bar must close there"
        return (
            f"A {word} triggers when a {timeframe} bar closes {above} {level:.{spec.digits}f} ({where}).",
            level,
        )
    period = p.pullback_rsi_period
    fast = float(rsi(features["close"], period).iloc[-1])
    threshold = p.pullback_buy_below if side > 0 else p.pullback_sell_above
    stretched = fast < threshold if side > 0 else fast > threshold
    if stretched:
        return (
            f"RSI({period}) is {fast:.0f}: a {word} triggers when a bar closes with it back {above} {threshold:g}.",
            None,
        )
    return (
        f"A {word} needs RSI({period}) to {'dip below' if side > 0 else 'rise above'} {threshold:g} "
        f"(now {fast:.0f}) and then turn back.",
        None,
    )
