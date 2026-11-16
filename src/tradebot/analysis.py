"""A descriptive market assessment for a discretionary trader.

This layer combines closed-bar evidence; it neither selects orders nor estimates a probability
of profit. Account balances, execution permissions and strategy entry blockers are not inputs.
Scenario levels are conditional structural hypotheses, not targets or stop-loss instructions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

import pandas as pd

from tradebot.config import TIMEFRAME_MINUTES

MAX_QUOTE_AGE_SECONDS = 120
MAX_CLOCK_LEAD_SECONDS = 60
"""A broker's server clock often runs a few seconds ahead of the PC's, so a fresh tick can carry a
time slightly later than "now". Up to this much is clock difference, not data from the future."""
STRUCTURE_BARS = 20
WEAK_ADX = 20.0
LIMITATIONS = (
    "Descriptive evidence, not a forecast or a probability of profit. Scenario levels are hypotheses "
    "to reassess on closed bars, not orders, targets or guaranteed stop prices. Quotes and levels "
    "come from the analysis feed; your execution broker may differ."
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC").tz_localize(None)
        return stamp.to_pydatetime()
    except (TypeError, ValueError, OverflowError):
        return None


def _direction(value: Any) -> int:
    value = _number(value)
    return 1 if value is not None and value > 0 else -1 if value is not None and value < 0 else 0


def _word(side: int) -> str:
    return "up" if side > 0 else "down" if side < 0 else "neutral"


def _unavailable(out: dict, status: str, reason: str) -> dict:
    out.update(status=status, regime=status, bias=0, headline=f"Analysis {status}: {reason}")
    out["conflicts"].append(reason)
    out["scenarios"] = []
    out["watch_next"] = ["Wait for fresh, complete closed-bar data before assessing a new scenario."]
    return out


def assess(
    features: pd.DataFrame,
    trend: Mapping[str, Any] | None,
    setups: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None,
    *,
    timeframe: str,
    bar_minutes: int,
    now: datetime,
    quote_time: datetime,
    price: float,
    digits: int,
    max_bar_age_bars: int = 2,
) -> dict[str, Any]:
    """Explain the current context using only supplied, completed bars.

    ``trend`` is ``trend.describe`` output; ``context`` is the symbol's market-view entry.
    Only BUY/SELL/SKIP/BLOCKED setup rows describe an actual closed-bar trigger. A WAIT row's
    suggested side is never counted as evidence. Setup counts do not determine analytical bias.
    """
    out: dict[str, Any] = {
        "status": "ready",
        "as_of": None,
        "valid_until": None,
        "headline": "",
        "regime": "mixed",
        "bias": 0,
        "evidence": [],
        "conflicts": [],
        "scenarios": [],
        "watch_next": [],
        "limitations": LIMITATIONS,
    }
    moment, quoted = _time(now), _time(quote_time)
    if moment is None or quoted is None or bar_minutes <= 0 or max_bar_age_bars <= 0:
        return _unavailable(out, "incomplete", "The clock or quote timestamp is unavailable.")
    if not isinstance(digits, int) or not 0 <= digits <= 10:
        return _unavailable(out, "incomplete", "Price precision is unavailable.")
    if quoted > moment + timedelta(seconds=MAX_CLOCK_LEAD_SECONDS):
        return _unavailable(out, "incomplete", "The quote is dated in the future; check the feed clock.")
    quoted = min(quoted, moment)
    if features.empty or "time" not in features:
        return _unavailable(out, "incomplete", "There are no closed bars to assess.")
    column = features["time"]
    if pd.api.types.is_datetime64_any_dtype(column):  # the usual case: checked without a Python loop
        if column.isna().any():
            return _unavailable(out, "incomplete", "A bar timestamp is missing or invalid.")
        ordered = column.is_monotonic_increasing and column.is_unique
        last = _time(column.iloc[-1])
    else:
        times = [_time(value) for value in column]
        if any(value is None for value in times):
            return _unavailable(out, "incomplete", "A bar timestamp is missing or invalid.")
        ordered = not any(a >= b for a, b in pairwise(times))
        last = times[-1]
    if not ordered:
        return _unavailable(out, "incomplete", "Bar timestamps are duplicated or out of order.")
    bar = timedelta(minutes=bar_minutes)
    closed_at = last + bar
    out["as_of"] = closed_at
    if closed_at > moment:
        return _unavailable(out, "incomplete", "The supplied latest bar has not closed yet.")
    out["valid_until"] = min(
        closed_at + bar * max_bar_age_bars,
        quoted + timedelta(seconds=MAX_QUOTE_AGE_SECONDS),
    )
    if moment - quoted > timedelta(seconds=MAX_QUOTE_AGE_SECONDS):
        return _unavailable(out, "stale", f"No fresh quote within {MAX_QUOTE_AGE_SECONDS} seconds.")
    if moment - closed_at > bar * max_bar_age_bars:
        return _unavailable(out, "stale", "The latest closed bar is too old for a current assessment.")
    if (_number(price) or 0) <= 0:
        return _unavailable(out, "incomplete", "The current quote is invalid.")

    row = features.iloc[-1]
    required = ("close", "high", "low", "atr", "ema_fast", "ema_slow", "adx", "macd_hist")
    values = {key: _number(row.get(key)) for key in required}
    if any(value is None for value in values.values()):
        return _unavailable(out, "incomplete", "Required price or indicator values are still unavailable.")
    close, high, low, atr = (values[key] for key in ("close", "high", "low", "atr"))
    if low <= 0 or atr <= 0 or not low <= close <= high or not 0 <= values["adx"] <= 100:
        return _unavailable(out, "incomplete", "The latest price range or indicator values are invalid.")

    chart = _direction(values["ema_fast"] - values["ema_slow"])
    momentum = _direction(values["macd_hist"])
    evidence, conflicts = out["evidence"], out["conflicts"]
    evidence.append(
        f"{timeframe} averages point {_word(chart)}; ADX is {values['adx']:.1f} "
        f"and MACD momentum points {_word(momentum)}."
    )
    higher = _higher(row, trend, bar_minutes)
    if higher:
        evidence.append(
            "Higher timeframes: " + ", ".join(f"{name} {_word(side)}" for name, side in higher) + "."
        )
    if not higher and bar_minutes < TIMEFRAME_MINUTES["D1"]:
        return _unavailable(out, "incomplete", "Higher-timeframe context is unavailable.")
    if any(side == 0 for _, side in higher):
        return _unavailable(out, "incomplete", "A higher timeframe has no established direction yet.")
    directions = {side for _, side in higher}
    aligned = next(iter(directions)) if len(directions) == 1 else chart if not higher else 0
    state = (trend or {}).get("state", "intact")
    if len(directions) > 1:
        regime, bias = "mixed", 0
        headline = "Higher timeframes disagree; directional evidence is mixed"
        conflicts.append("The higher timeframes point in opposite directions.")
    elif values["adx"] < WEAK_ADX or chart == 0:
        regime, bias = "weak trend", 0
        headline = "Directional evidence is weak; a trading range is not yet established"
        conflicts.append(f"ADX below {WEAK_ADX:g} or flat averages weakens a directional interpretation.")
    elif state in {"weakening", "ending"}:
        regime, bias = "transition", 0
        headline = f"The {timeframe} trend is weakening; continuation and reversal remain open"
        conflicts.append("The existing trend has warning signs; that does not establish the next direction.")
    elif aligned and (
        chart == -aligned or momentum == -aligned or (close - values["ema_fast"]) * aligned < 0
    ):
        regime, bias = "pullback", aligned
        headline = f"Higher timeframes point {_word(aligned)}, with a possible pullback on {timeframe}"
        conflicts.append(
            "Short-term price or momentum opposes the broader direction; continuation is unconfirmed."
        )
    elif aligned and chart == aligned and momentum == aligned:
        regime, bias = "aligned trend", aligned
        headline = f"Trend and momentum are aligned {_word(aligned)}; watch the next structural level"
    else:
        regime, bias = "mixed", 0
        headline = "Trend and momentum do not give a consistent directional picture"
        conflicts.append("Some directional inputs are neutral or disagree.")
    out.update(regime=regime, bias=bias, headline=headline)
    for sign in (trend or {}).get("signs", [])[:3]:
        if isinstance(sign, Mapping) and sign.get("text"):
            conflicts.append(str(sign["text"]))
    rsi = _number(row.get("rsi"))
    if rsi is not None and (rsi >= 70 or rsi <= 30):
        conflicts.append(f"RSI is {rsi:.1f}; stretched momentum alone does not confirm a reversal.")
    _setup_evidence(setups, evidence, conflicts, bias)
    _context_evidence(context or {}, evidence, conflicts)
    out["scenarios"] = _scenarios(features, context or {}, close, timeframe, digits)
    if out["scenarios"]:
        out["watch_next"] = [
            "Watch which structural boundary a completed bar crosses; an intrabar touch is unconfirmed.",
            "Reassess if higher timeframes disagree or momentum changes direction.",
        ]
    else:
        conflicts.append("There is not enough valid closed-bar structure to name both scenario boundaries.")
        out["watch_next"] = ["Wait for enough closed-bar structure to define confirmation and invalidation."]
    return out


def _higher(row: pd.Series, trend: Mapping[str, Any] | None, bar_minutes: int) -> list[tuple[str, int]]:
    columns = [name for name in row.index if isinstance(name, str) and name.startswith("trend_")]
    if columns:
        return [
            (name[6:], _direction(row[name]))
            for name in sorted(columns)
            if TIMEFRAME_MINUTES.get(name[6:], 0) > bar_minutes
        ]
    return [
        (str(item.get("timeframe", "")), _direction(item.get("direction")))
        for item in (trend or {}).get("higher", [])
        if isinstance(item, Mapping)
        and TIMEFRAME_MINUTES.get(str(item.get("timeframe", "")), 0) > bar_minutes
    ]


def _setup_evidence(
    setups: Sequence[Mapping[str, Any]], evidence: list[str], conflicts: list[str], bias: int = 0
) -> None:
    """Closed-bar triggers as evidence. A trigger against the directional picture is a conflict
    too: on 16 Nov 2026 a pullback buy fired in a strong downtrend and the card said "No
    directional conflict", because only triggers that disagreed with each other were counted."""
    fired: dict[str, int] = {}
    for setup in setups:
        if setup.get("action") in {"BUY", "SELL", "SKIP", "BLOCKED"}:
            side = _direction(setup.get("side"))
            if side:
                fired[str(setup.get("label") or setup.get("name") or "Setup")] = side
    if fired:
        description = ", ".join(f"{name} {_word(side)}" for name, side in sorted(fired.items()))
        evidence.append(
            f"Closed-bar setup triggers: {description}. These share inputs and are not independent votes."
        )
        if len(set(fired.values())) > 1:
            conflicts.append("The named setup triggers point in different directions.")
        against = sorted(name for name, side in fired.items() if bias and side == -bias)
        if against:
            conflicts.append(
                f"{', '.join(against)} triggered {_word(-bias)}, against the {_word(bias)} picture above."
            )


def _context_evidence(context: Mapping[str, Any], evidence: list[str], conflicts: list[str]) -> None:
    volatility = context.get("volatility") or {}
    percentile = _number(volatility.get("percentile"))
    if percentile is not None and 0 <= percentile <= 100:
        evidence.append(
            f"ATR is at the {percentile:.0f}th percentile of its recent history; this measures activity, not direction."
        )
    levels = context.get("levels") or {}
    used = _number(levels.get("range_used"))
    if used is not None and used >= 100:
        conflicts.append(
            f"The forming day's range is {used:.0f}% of its recent daily average; that average is not a price ceiling."
        )


def _scenarios(
    features: pd.DataFrame, context: Mapping[str, Any], close: float, timeframe: str, digits: int
) -> list[dict]:
    if len(features) < 2 or not {"high", "low"}.issubset(features.columns):
        return []
    recent = features.tail(STRUCTURE_BARS)
    highs, lows = [_number(v) for v in recent["high"]], [_number(v) for v in recent["low"]]
    if any(value is None for value in [*highs, *lows]) or any(
        low <= 0 or low > high for low, high in zip(lows, highs)
    ):
        return []
    candidates = [(f"{len(recent)}-bar high", max(highs)), (f"{len(recent)}-bar low", min(lows))]
    historical = {
        "Previous day high",
        "Previous day low",
        "Previous day close",
        "Previous week high",
        "Previous week low",
        "Pivot",
        "R1",
        "S1",
        "R2",
        "S2",
    }
    for item in (context.get("levels") or {}).get("levels", []):
        value = _number(item.get("price"))
        if (
            item.get("name") in historical
            and value is not None
            and value > 0
            and round(value, digits) != round(close, digits)
        ):
            candidates.append((str(item["name"]), value))
    above, below = [p for p in candidates if p[1] >= close], [p for p in candidates if p[1] <= close]
    if not above or not below:
        return []
    upper, lower = min(above, key=lambda p: p[1]), max(below, key=lambda p: p[1])
    high, low = round(upper[1], digits), round(lower[1], digits)
    if low >= high:
        return []
    scenarios = []
    for side, label, level, opposite, source in (
        (1, "Bullish scenario", high, low, upper[0]),
        (-1, "Bearish scenario", low, high, lower[0]),
    ):
        beyond, back = ("above", "below") if side > 0 else ("below", "above")
        scenarios.append(
            {
                "side": side,
                "label": label,
                "level": level,
                "invalidation_level": opposite,
                "confirmation": f"A completed {timeframe} bar closes {beyond} {level:.{digits}f} ({source.lower()}).",
                "invalidation": f"A completed {timeframe} bar closes {back} {opposite:.{digits}f}, the opposite structural boundary.",
            }
        )
    return scenarios


def format_text(symbol: str, assessment: Mapping[str, Any]) -> str:
    """The same assessment in a compact format suitable for the phone."""
    lines = [f"{symbol} analysis: {assessment['headline']}"]
    if assessment.get("as_of"):
        moment = _time(assessment["as_of"])
        if moment:
            lines.append(f"Closed-bar evidence as of {moment:%Y-%m-%d %H:%M} UTC.")
    lines.extend(assessment.get("evidence", [])[:3])
    lines.extend(f"Watch: {line}" for line in assessment.get("conflicts", [])[:3])
    for scenario in assessment.get("scenarios", []):
        lines.append(
            f"{scenario['label']}: {scenario['confirmation']} Invalidation: {scenario['invalidation']}"
        )
    lines.extend(assessment.get("watch_next", [])[:1])
    lines.append(str(assessment.get("limitations", LIMITATIONS)))
    return "\n".join(lines)


def semantic_key(assessment: Mapping[str, Any]) -> tuple[str, str, int]:
    """Alert only for an assessment-state change, not every quote, timestamp or moving level."""
    return str(assessment["status"]), str(assessment["regime"]), int(assessment["bias"])
