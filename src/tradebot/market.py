"""The market as a whole: what one instrument's chart does not show.

Every figure here is a description of what has happened, on closed bars: which currencies have
moved, how the instruments move together, what you are exposed to across all open trades, where
yesterday's and this week's levels are, how much of a normal day's range is already used. None of
it is a signal. What the two most quoted of them have been worth as predictions was measured on
the stored history and is reported in the README (``scripts/research_market_context.py``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tradebot.broker.base import Position, SymbolSpec
from tradebot.config import session_hours

CORRELATION_BARS = 500
CORRELATED = 0.7
ADR_DAYS = 20
WEEK_SHIFT = pd.Timedelta(hours=3)
"""Daily bars open at 17:00 New York, which is Sunday evening in UTC: shifted by three hours,
Sunday's bar lands on Monday and a calendar week is the trading week."""


def legs(spec: SymbolSpec) -> list[tuple[str, int]]:
    """What a long position in this instrument is long (+1) and short (-1) of.

    EURUSD is long EUR and short USD. An index or any instrument quoted in its own currency is
    just long itself: buying US500 is not a position against the dollar.
    """
    base, quote = spec.currency_base.upper(), spec.currency_profit.upper()
    if not base or not quote or base == quote:
        return [(spec.name, 1)]
    return [(base, 1), (quote, -1)]


def strength(
    features: Mapping[str, pd.DataFrame], specs: Mapping[str, SymbolSpec], bars: int
) -> dict[str, float]:
    """How far each currency has moved over the last ``bars`` bars, averaged over its instruments.

    Each instrument's move is divided by its own ATR times sqrt(bars), so gold and EURUSD count
    the same and the score reads like a z-score: beyond about +/-1 is a clear move.
    """
    scores: dict[str, list[float]] = defaultdict(list)
    for symbol, frame in features.items():
        spec = specs.get(symbol)
        if spec is None or len(frame) <= bars:
            continue
        close, atr = frame["close"].to_numpy(dtype=float), float(frame["atr"].iloc[-1])
        if not (atr > 0 and math.isfinite(atr)):
            continue
        move = (close[-1] - close[-1 - bars]) / (atr * math.sqrt(bars))
        for asset, sign in legs(spec):
            scores[asset].append(sign * move)
    return {asset: float(np.mean(values)) for asset, values in scores.items()}


def strength_table(
    features: Mapping[str, pd.DataFrame], specs: Mapping[str, SymbolSpec], bar_minutes: int
) -> list[dict[str, Any]]:
    """Currencies from strongest to weakest over the last day, with the last week beside it."""
    day_bars = max(2, 1440 // bar_minutes)
    day, week = strength(features, specs, day_bars), strength(features, specs, 5 * day_bars)
    count: dict[str, int] = defaultdict(int)
    for symbol in features:
        for asset, _ in legs(specs[symbol]) if symbol in specs else []:
            count[asset] += 1
    rows = [{"asset": a, "day": day[a], "week": week.get(a), "pairs": count[a]} for a in day]
    return sorted(rows, key=lambda r: -r["day"])


def correlation(features: Mapping[str, pd.DataFrame], bars: int = CORRELATION_BARS) -> dict[str, Any] | None:
    """Correlation of bar-to-bar returns over the bars every instrument has in common."""
    closes = {s: f.set_index("time")["close"] for s, f in features.items() if len(f)}
    if len(closes) < 2:
        return None
    frame = pd.DataFrame(closes).dropna().tail(bars + 1)
    returns = np.log(frame).diff().dropna()
    if len(returns) < 50:
        return None
    matrix = returns.corr().to_numpy()
    return {
        "symbols": list(frame.columns),
        "matrix": [[None if math.isnan(v) else round(float(v), 2) for v in row] for row in matrix],
        "bars": len(returns),
    }


def notional(position: Position, spec: SymbolSpec, price: float) -> float:
    """Account-currency value of what the position controls (a tick is worth ``tick_value``)."""
    return position.volume * price / spec.tick_size * spec.tick_value


def exposure(
    positions: Sequence[Position],
    specs: Mapping[str, SymbolSpec],
    prices: Mapping[str, float],
    equity: float,
) -> list[dict[str, Any]]:
    """Net long (+) or short (-) amount per currency across every open position, largest first."""
    totals: dict[str, float] = defaultdict(float)
    through: dict[str, list[str]] = defaultdict(list)
    for p in positions:
        spec = specs.get(p.symbol)
        if spec is None:
            continue
        amount = notional(p, spec, prices.get(p.symbol) or p.price_open)
        for asset, sign in legs(spec):
            totals[asset] += p.side * sign * amount
            through[asset].append(p.symbol)
    rows = [
        {"asset": asset, "amount": amount, "percent": amount / equity * 100 if equity > 0 else None,
         "through": sorted(set(through[asset]))}
        for asset, amount in totals.items()
    ]  # fmt: skip
    return sorted(rows, key=lambda r: -abs(r["amount"]))


def overlap(symbol: str, side: int, specs: Mapping[str, SymbolSpec], positions: Sequence[Position]) -> str:
    """What a new trade on ``symbol`` would add to: the same bet you already hold elsewhere."""
    spec = specs.get(symbol)
    if spec is None:
        return ""
    held: dict[tuple[str, int], list[str]] = defaultdict(list)
    for p in positions:
        other = specs.get(p.symbol)
        if other is None or p.symbol == symbol:
            continue
        for asset, sign in legs(other):
            held[(asset, p.side * sign)].append(f"{'buy' if p.side > 0 else 'sell'} {p.symbol}")
    parts = []
    for asset, sign in legs(spec):
        same = held.get((asset, side * sign))
        if same:
            parts.append(f"{'long' if side * sign > 0 else 'short'} {asset} (your {', '.join(same)})")
    if not parts:
        return ""
    word = "buy" if side > 0 else "sell"
    return (
        f"A {word} here adds to a bet you already hold: {' and '.join(parts)}. "
        f"If that currency moves against you, these trades lose together."
    )


def correlated_positions(positions: Sequence[Position], corr: dict[str, Any] | None) -> list[str]:
    """Pairs of open positions that have been moving as one trade, or cancelling each other."""
    if not corr:
        return []
    index = {s: i for i, s in enumerate(corr["symbols"])}
    out = []
    for i, a in enumerate(positions):
        for b in positions[i + 1 :]:
            if a.symbol == b.symbol or a.symbol not in index or b.symbol not in index:
                continue
            value = corr["matrix"][index[a.symbol]][index[b.symbol]]
            if value is None or abs(value) < CORRELATED:
                continue
            together = value * a.side * b.side > 0
            names = f"{_word(a)} {a.symbol} and {_word(b)} {b.symbol}"
            out.append(
                f"{names} have moved {'together' if together else 'against each other'} "
                f"(correlation {value:+.2f} over {corr['bars']} bars): "
                + ("count them as one larger position." if together else "they largely cancel out.")
            )
    return out


def _word(position: Position) -> str:
    return "buy" if position.side > 0 else "sell"


def volatility(features: pd.DataFrame, lookback: int = CORRELATION_BARS) -> dict[str, Any] | None:
    """Where the current ATR stands among its own recent values."""
    atr = features["atr"].dropna().tail(lookback)
    if len(atr) < 50:
        return None
    now = float(atr.iloc[-1])
    percentile = float((atr < now).mean() * 100)
    label = "quiet" if percentile < 25 else "busy" if percentile > 75 else "normal"
    return {"atr": now, "percentile": percentile, "label": label, "bars": len(atr)}


def key_levels(
    daily: pd.DataFrame | None, today: Mapping[str, Any] | None, price: float, atr: float
) -> dict[str, Any] | None:
    """Yesterday's and this week's levels, the classic pivots, and how much of a normal day is used.

    ``daily`` holds closed daily bars, oldest first; ``today`` is the daily bar still forming, or
    None while the market is closed.
    """
    if daily is None or len(daily) < 2 or not atr > 0:
        return None
    prev = daily.iloc[-1]
    high, low, close = float(prev["high"]), float(prev["low"]), float(prev["close"])
    pivot = (high + low + close) / 3
    levels = [
        ("Previous day high", high), ("Previous day low", low), ("Previous day close", close),
        ("Pivot", pivot), ("R1", 2 * pivot - low), ("S1", 2 * pivot - high),
        ("R2", pivot + high - low), ("S2", pivot - high + low),
    ]  # fmt: skip
    week = (daily["time"] + WEEK_SHIFT).dt.to_period("W")
    current = week.iloc[-1]
    day_high = day_low = None
    if today is not None:
        current = (pd.Timestamp(today["time"]) + WEEK_SHIFT).to_period("W")
        day_high, day_low = float(today["high"]), float(today["low"])
        levels.append(("Today's open", float(today["open"])))
    this_week = daily[week == current]
    highs = [*this_week["high"].tolist(), *([day_high] if day_high is not None else [])]
    lows = [*this_week["low"].tolist(), *([day_low] if day_low is not None else [])]
    if highs:
        levels += [("This week's high", max(highs)), ("This week's low", min(lows))]
    before = daily[week < current]
    if len(before):
        last_week = before[week[before.index] == week[before.index].iloc[-1]]
        levels += [("Previous week high", float(last_week["high"].max())),
                   ("Previous week low", float(last_week["low"].min()))]  # fmt: skip
    rows: list[dict[str, Any]] = [
        {"name": name, "price": value, "away_atr": (value - price) / atr} for name, value in levels
    ]
    rows.sort(key=lambda r: -r["price"])
    recent = daily.tail(ADR_DAYS)
    adr = float((recent["high"] - recent["low"]).mean())
    day_range = day_high - day_low if day_high is not None and day_low is not None else None
    return {
        "levels": rows,
        "nearest": min(rows, key=lambda r: abs(r["away_atr"])),
        "adr": adr,
        "adr_days": len(recent),
        "day_high": day_high,
        "day_low": day_low,
        "day_range": day_range,
        "range_used": day_range / adr * 100 if day_range is not None and adr > 0 else None,
    }


SESSION_NAMES = (("asia", "Asia"), ("london", "London"), ("new_york", "New York"))


def fx_weekend(now: datetime) -> bool:
    """Forex is shut from Friday 17:00 to Sunday 17:00 New York time (``now`` is UTC). Counting the
    whole of Sunday as closed showed the Asia session closed for its first hours every week."""
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    day, hour = local.weekday(), local.hour
    return day == 5 or (day == 4 and hour >= 17) or (day == 6 and hour < 17)


def sessions(now: datetime) -> list[dict[str, Any]]:
    """Which of the three main sessions are open at ``now`` (UTC), by the bot's own session hours."""
    out = []
    weekend = fx_weekend(now)
    for key, label in SESSION_NAMES:
        start, end = session_hours(key, now)
        inside = start <= now.hour < end if start < end else (now.hour >= start or now.hour < end)
        out.append({"name": label, "open": inside and not weekend, "hours": f"{start:02d}-{end:02d} UTC"})
    return out


def summary(table: list[dict[str, Any]]) -> str:
    """One line on the strongest and weakest currency of the last day."""
    if len(table) < 2:
        return ""
    top, bottom = table[0], table[-1]
    return (
        f"Over the last day {top['asset']} moved most upwards ({top['day']:+.1f}) and {bottom['asset']} "
        f"most downwards ({bottom['day']:+.1f}), in ATR-scaled units."
    )
