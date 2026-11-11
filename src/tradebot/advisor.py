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
from tradebot.strategy import BLOCK_LABELS, FILTERS, _near_fib, explain, trend_columns

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
    """BUY / SELL (take it now), SKIP (signal, but a guard says no), BLOCKED (a trigger fired on
    the last bar and a strategy filter rejected it), WAIT, CLOSED."""
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
    direction_note: str = ""
    """Why only one direction is being considered, when the chart may look like the other."""
    plans: dict[str, TradePlan] = field(default_factory=dict)
    """The plan for each side ("buy", "sell") at the live quote, for a trade the user chooses."""
    last_trigger: dict | None = None
    """The most recent entry trigger in the last few bars, taken or filtered, and the move since."""
    early: dict | None = None
    """What the bar that is still forming would do: see ``early_warning``."""
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
    raw, block = features["raw_signal"].to_numpy(), features["block"].to_numpy()
    high, low, close, atr_ = (features[k].to_numpy(dtype=float) for k in ("high", "low", "close", "atr"))
    bar = pd.Timedelta(minutes=bar_minutes)
    passed = np.flatnonzero(signal != 0)
    tradeable = [i for i in passed if schedule.closed((time_.iloc[i] + bar).to_pydatetime()) is None]
    fired = np.flatnonzero((signal != 0) | (raw != 0))  # blocked triggers are listed too
    items = []
    for i in fired[::-1][:limit]:
        side, entry = int(signal[i] or raw[i]), close[i]
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
             "outcome": outcome, "r": r, "blocked": schedule.closed(closed_at),
             "filter": "" if signal[i] else BLOCK_LABELS.get(block[i], str(block[i]))}
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
    preview: pd.DataFrame | None = None,
) -> Advice:
    """``preview`` (optional) is the same features with the forming bar appended at the current price."""
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
    plans = {
        "buy": trade_plan(engine, symbol, spec, params, row, tick, 1),
        "sell": trade_plan(engine, symbol, spec, params, row, tick, -1),
    }
    plan = plans["buy" if side > 0 else "sell"]
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
        "direction_note": direction_note(row, p, side, config.timeframe),
        "plans": plans,
        "last_trigger": last_trigger(features, params, tick, config.bar_minutes),
        "early": early_warning(features, preview, params, spec, config.bar_minutes),
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
        word = WORD[raw].title()
        return Advice(
            symbol, "BLOCKED", f"{word} trigger fired, blocked by a filter",
            f"The {word.lower()} trigger fired on the last bar, but the bot did not take it: {label}. "
            f"The plan shows the {word.lower()} if you want to take it on your own call.",
            actionable=False, **common,
        )  # fmt: skip
    recent = common["last_trigger"]
    if recent and recent["bars_ago"] > 0:
        word = WORD[recent["side"]].title()
        ago = f"{recent['bars_ago']} bar{'' if recent['bars_ago'] == 1 else 's'} ago"
        outcome = "taken" if recent["taken"] else f"blocked ({recent['blocked_by']})"
        return Advice(symbol, "WAIT", f"{word} trigger {ago}, {outcome}", trigger, actionable=False, **common)
    return Advice(symbol, "WAIT", "No setup yet", trigger, actionable=False, **common)


def cross_level(features: pd.DataFrame, p: StrategyParams) -> tuple[int, float] | None:
    """For the EMA crossover: which way the averages can cross next, and the close that does it.

    With alpha = 2 / (n + 1), a bar closing at c moves an EMA to alpha * c + (1 - alpha) * previous.
    Setting the new fast and slow EMAs equal and solving for c gives the exact closing price at
    which the forming bar produces a cross.
    """
    if p.name != "ema_cross":
        return None
    fast, slow = float(features["ema_fast"].iloc[-1]), float(features["ema_slow"].iloc[-1])
    if not (np.isfinite(fast) and np.isfinite(slow)) or fast == slow:
        return None
    a_fast, a_slow = 2 / (p.fast_ema + 1), 2 / (p.slow_ema + 1)
    level = ((1 - a_slow) * slow - (1 - a_fast) * fast) / (a_fast - a_slow)
    return (-1 if fast > slow else 1), level


def early_warning(
    features: pd.DataFrame,
    preview: pd.DataFrame | None,
    params: SymbolParams,
    spec: SymbolSpec,
    bar_minutes: int,
) -> dict | None:
    """A heads-up about the bar that is still forming. It is NOT a signal.

    ``firing`` means a trigger would fire if the bar closed at the current price; it can vanish
    before the close, which is why the bot only ever acts on closed bars. ``level`` is the closing
    price that produces the trigger (EMA crossover only), so a setup can be watched as it builds.
    """
    out: dict = {"firing": False, "side": 0, "level": None, "passes": None, "blocked_by": "",
                 "closes_at": None, "price": None}  # fmt: skip
    cross = cross_level(features, params.strategy)
    if cross:
        out["side"], out["level"] = cross[0], spec.round_price(cross[1])
    if preview is not None and len(preview):
        row = preview.iloc[-1]
        out["closes_at"] = (row["time"] + pd.Timedelta(minutes=bar_minutes)).to_pydatetime()
        out["price"] = float(row["close"])
        raw = int(row["raw_signal"])
        if raw:
            passes = bool(row["signal"] != 0)
            out.update(firing=True, side=raw, passes=passes,
                       blocked_by="" if passes else BLOCK_LABELS.get(row["block"], str(row["block"])))  # fmt: skip
    return out if out["firing"] or out["level"] is not None else None


TRIGGER_LOOKBACK = 12


def last_trigger(features: pd.DataFrame, params: SymbolParams, tick: Tick, bar_minutes: int) -> dict | None:
    """The latest entry trigger within the last 12 bars, whether the filters passed it or not.

    A filtered trigger used to vanish from the screen after one bar. Keeping it visible, with the
    filter that blocked it and what price has done since, lets a person judge the filter's call.
    """
    recent = features.tail(TRIGGER_LOOKBACK)
    fired = recent[recent["raw_signal"] != 0]
    if fired.empty:
        return None
    row = fired.iloc[-1]
    side = int(row["raw_signal"])
    price = float(row["close"])
    risk = params.risk.sl_atr * float(row["atr"])
    now_price = tick.bid if side > 0 else tick.ask  # where the position could be closed
    return {
        "time": (row["time"] + pd.Timedelta(minutes=bar_minutes)).to_pydatetime(),
        "bars_ago": len(features) - 1 - int(fired.index[-1]),
        "side": side,
        "price": price,
        "taken": bool(row["signal"] != 0),
        "blocked_by": "" if row["signal"] != 0 else BLOCK_LABELS.get(row["block"], str(row["block"])),
        "r_since": (now_price - price) * side / risk if risk else 0.0,
    }


def direction_note(row: pd.Series, p: StrategyParams, side: int, timeframe: str) -> str:
    """Explain a trend-filtered direction, and say so plainly when the chart is moving against it."""
    if not (p.use_trend_filter and "trend" in FILTERS[p.name]):
        return ""
    columns = trend_columns(row)
    names = [c[6:] for c in columns] or [p.trend_timeframe]
    label, several = " and ".join(names), len(names) > 1
    if several and int(row["trend"]) == 0 and len({int(row[c]) for c in columns}) > 1:
        states = ", ".join(
            f"{c[6:]} {'up' if row[c] > 0 else 'down' if row[c] < 0 else 'flat'}" for c in columns
        )
        return (
            f"No trades either way right now: the higher timeframes disagree ({states}). "
            f"A trigger is only taken when {label} point the same way."
        )
    if int(row["trend"]) != side:
        return ""
    allowed, other = ("buys", "sell") if side > 0 else ("sells", "buy")
    note = (
        f"Only {allowed} are considered here: the {label} trend{'s are' if several else ' is'} "
        f"{'up' if side > 0 else 'down'} (the last completed {label} bar{'s' if several else ''} closed "
        f"{'above' if side > 0 else 'below'} {'their' if several else 'its'} EMA {p.trend_ema})."
    )
    chart_up = row["ema_fast"] > row["ema_slow"]
    if chart_up != (side > 0):
        note += (
            f" The {timeframe} chart is moving {'up' if chart_up else 'down'}, against that trend, so {other} "
            f"triggers are filtered out until the {label} trend{'s turn' if several else ' turns'}."
        )
    return note


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
        columns = [(c[6:], c) for c in trend_columns(features)] or [(p.trend_timeframe, "trend")]
        for name, column in columns:  # one row per higher timeframe; all must agree with the trade
            value = int(row[column])
            state = {1: "up", -1: "down"}.get(value, "flat")
            checks.append(Check(f"{name} trend", value == side, f"{state} (close vs EMA {p.trend_ema})"))
    if "adx" in applicable and p.use_adx:
        checks.append(Check("Trend strength", bool(row["adx"] >= p.adx_min),
                            f"ADX {row['adx']:.1f}, needs ≥ {p.adx_min:g}"))  # fmt: skip
    if "macd" in applicable and p.use_macd:
        hist = float(row["macd_hist"])
        checks.append(Check("Momentum", bool(np.sign(hist) == side),
                            f"MACD histogram {'positive' if hist > 0 else 'negative'}, "
                            f"needs {'positive to buy' if side > 0 else 'negative to sell'}"))  # fmt: skip
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
