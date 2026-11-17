"""Market structure, support and resistance zones, and chart patterns, read from closed bars.

This is context for a person reading the chart, not a signal. Each of these was traded as an entry
rule on the stored H1 history (``scripts/research_structure.py``) and none made money once the
spread was paid, so the dashboard shows them with that record and the bot never trades on them.

Swing points: a bar whose high (low) is the highest (lowest) of the ``SWING_BARS`` bars on either
side. A swing is only known ``SWING_BARS`` bars later, so the newest bars never hold one, and
everything below at bar i uses only swings confirmed by bar i. Highs and lows alternate: of two
highs in a row the higher is kept, of two lows the lower.

Structure: the last two swing highs and the last two swing lows. Both higher = uptrend (HH, HL),
both lower = downtrend (LH, LL), anything else = range. A close through the last swing low of an
uptrend (high of a downtrend) is a break of structure, the first sign the sequence has ended.

Zones: swing highs and lows within ``ZONE_TOLERANCE_ATR`` ATR of each other form one zone;
two touches or more make it count. A zone with both highs and lows in it has changed role
(support that became resistance, or the reverse). Round numbers are listed alongside.

Patterns, from the latest swings (prices within ``EQUAL_ATR`` ATR count as equal):
  double top / bottom   two equal highs (lows), neckline at the low (high) between them
  head and shoulders    three highs, the middle one highest, shoulders within 1 ATR; neckline
                        through the two lows between them (inverse: the mirror image)
  triangles             the last two highs and two lows converging: falling highs and rising
                        lows (symmetrical), equal highs and rising lows (ascending), falling highs
                        and equal lows (descending)
A pattern is "forming" until a bar closes through its neckline (or triangle line), "confirmed"
after, and gives the measured target the textbooks use: the pattern's height from the break.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

SWING_BARS = 3
ZONE_TOLERANCE_ATR = 0.5
EQUAL_ATR = 0.5
ZONE_LOOKBACK = 300
SWINGS_SHOWN = 80
"""Swings labelled on the chart: enough for its widest view."""
PATTERN_RECENT_BARS = 60
"""A pattern whose last swing is older than this is history, not the current picture."""
CONFIRMED_RECENT_BARS = 20
"""A confirmed pattern is shown for this many bars after its break."""

RECORD = {
    "double_top": "As an entry, selling these neckline breaks lost 0.08R to 0.10R a trade in the stored history.",
    "double_bottom": "As an entry, buying these neckline breaks lost 0.08R to 0.10R a trade in the stored history.",
    "head_shoulders": "As an entry, trading these neckline breaks lost 0.06R to 0.08R a trade in the stored history.",
    "inverse_head_shoulders": "As an entry, trading these neckline breaks lost 0.06R to 0.08R a trade in the stored history.",
    "triangle": "As an entry, trading triangle breaks lost 0.08R to 0.09R a trade in the stored history.",
    "structure_break": "As an entry, trading breaks of structure lost 0.08R to 0.09R a trade in the stored history.",
    "round_ahead": "Bot signals with a round number less than half an ATR in the way did worse (-0.2R to -0.35R "
    "a trade), but only 30 such trades were measured.",
}
"""Measured by ``scripts/research_structure.py`` on 2026-11-17: six symbols, H1, 2017-2022 and
2023-2026 separately, inside the trading hours, with the bot's stops and costs. Shown next to each
item so that a pattern on the chart is never read as an edge. With the trend filter on, results
were no better (-0.06R to -0.30R, except head and shoulders: +0.11R on 40 trades since 2023 after
-0.21R before). A zone within 1 ATR in a signal's way changed nothing consistently."""

NAMES = {
    "double_top": "Double top",
    "double_bottom": "Double bottom",
    "head_shoulders": "Head and shoulders",
    "inverse_head_shoulders": "Inverse head and shoulders",
    "ascending_triangle": "Ascending triangle",
    "descending_triangle": "Descending triangle",
    "symmetrical_triangle": "Symmetrical triangle",
}


@dataclass(frozen=True)
class Swing:
    i: int
    """Position of the swing bar in the frame."""
    kind: str
    """"high" or "low"."""
    price: float
    label: str = ""
    """HH, LH, EH (equal high), HL, LL, EL, or "" for the first of its kind."""


def swings(features: pd.DataFrame, upto: int | None = None, k: int = SWING_BARS) -> list[Swing]:
    """Alternating swing highs and lows confirmed by bar ``upto`` (default: the last bar)."""
    n = len(features) if upto is None else upto + 1
    if n < 2 * k + 1:
        return []
    high = features["high"].to_numpy(dtype=float)[:n]
    low = features["low"].to_numpy(dtype=float)[:n]
    atr = features["atr"].to_numpy(dtype=float)[:n]
    window = 2 * k + 1
    top = pd.Series(high).rolling(window, center=True).max().to_numpy()
    bottom = pd.Series(low).rolling(window, center=True).min().to_numpy()
    raw: list[Swing] = []
    for j in range(k, n - k):  # bar j is confirmed once bar j + k has closed
        is_high, is_low = high[j] == top[j], low[j] == bottom[j]
        if is_high and is_low:  # an outside bar: keep the side that continues the alternation
            is_low = bool(raw) and raw[-1].kind == "high"
            is_high = not is_low
        for kind, price, hit in (("high", high[j], is_high), ("low", low[j], is_low)):
            if not hit:
                continue
            if raw and raw[-1].kind == kind:  # two of a kind in a row: keep the more extreme
                better = price > raw[-1].price if kind == "high" else price < raw[-1].price
                if better:
                    raw[-1] = Swing(j, kind, float(price))
                continue
            raw.append(Swing(j, kind, float(price)))
    out: list[Swing] = []
    last: dict[str, Swing] = {}
    for s in raw:
        before = last.get(s.kind)
        label = ""
        if before is not None:
            tolerance = 0.1 * atr[s.i] if math.isfinite(atr[s.i]) else 0.0
            if abs(s.price - before.price) <= tolerance:
                label = "EH" if s.kind == "high" else "EL"
            elif s.price > before.price:
                label = "HH" if s.kind == "high" else "HL"
            else:
                label = "LH" if s.kind == "high" else "LL"
        s = Swing(s.i, s.kind, s.price, label)
        out.append(s)
        last[s.kind] = s
    return out


def structure_state(points: list[Swing], close: float) -> dict[str, Any]:
    """Uptrend, downtrend or range from the last two highs and lows, and a break of structure."""
    highs = [s for s in points if s.kind == "high"][-2:]
    lows = [s for s in points if s.kind == "low"][-2:]
    out: dict[str, Any] = {"state": "unknown", "last_high": None, "last_low": None, "broken": 0}
    if len(highs) < 2 or len(lows) < 2:
        return out
    out["last_high"], out["last_low"] = highs[-1].price, lows[-1].price
    rising = highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price
    falling = highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price
    out["state"] = "up" if rising else "down" if falling else "range"
    if rising and close < lows[-1].price:
        out["broken"] = -1  # an uptrend closed below its last higher low
    elif falling and close > highs[-1].price:
        out["broken"] = 1  # a downtrend closed above its last lower high
    return out


def zones(points: list[Swing], atr: float, price: float, since: int = 0) -> list[dict[str, Any]]:
    """Clusters of two or more swing prices within ``ZONE_TOLERANCE_ATR`` ATR, nearest first."""
    if not (math.isfinite(atr) and atr > 0):
        return []
    tolerance = ZONE_TOLERANCE_ATR * atr
    recent = sorted((s for s in points if s.i >= since), key=lambda s: s.price)
    groups: list[list[Swing]] = []
    for s in recent:
        if groups and s.price - groups[-1][0].price <= tolerance:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for group in groups:
        if len(group) < 2:
            continue
        low, high = group[0].price, group[-1].price
        kinds = {s.kind for s in group}
        side = "resistance" if low > price else "support" if high < price else "inside"
        out.append({
            "low": low, "high": high, "touches": len(group), "side": side,
            "flipped": len(kinds) == 2,  # highs and lows both turned here: support became resistance or back
            "last": max(s.i for s in group),
            "distance_atr": 0.0 if side == "inside" else min(abs(price - low), abs(price - high)) / atr,
        })  # fmt: skip
    return sorted(out, key=lambda z: z["distance_atr"])


def round_levels(price: float) -> dict[str, float]:
    """The psychological levels either side of ``price``: 1.10 and 1.15 on EURUSD, 150 and 155 on
    USDJPY, 4000 and 4050 on gold (half a unit of the price's second digit)."""
    if not (math.isfinite(price) and price > 0):
        return {}
    step = 10 ** (math.floor(math.log10(price)) - 1) * 0.5
    below = math.floor(price / step) * step
    return {"below": round(below, 10), "above": round(below + step, 10), "step": step}


def _line(a: Swing, b: Swing, i: float) -> float:
    return a.price + (b.price - a.price) * (i - a.i) / (b.i - a.i) if b.i != a.i else a.price


def _first_close_through(close: np.ndarray, start: int, end: int, level, side: int) -> int | None:
    """First bar in (start, end] that closed beyond ``level(i)`` in direction ``side``."""
    for i in range(start + 1, end + 1):
        if (close[i] - level(i)) * side > 0:
            return i
    return None


def patterns(features: pd.DataFrame, points: list[Swing], upto: int | None = None) -> list[dict[str, Any]]:
    """Chart patterns in the latest swings, as of bar ``upto`` (default: the last bar)."""
    end = len(features) - 1 if upto is None else upto
    if end < 0 or len(points) < 3:
        return []
    close = features["close"].to_numpy(dtype=float)
    atr = float(features["atr"].iat[end])
    if not (math.isfinite(atr) and atr > 0):
        return []
    equal = EQUAL_ATR * atr
    found: list[dict[str, Any]] = []
    known_at = points[-1].i + SWING_BARS  # the bar on which the newest swing was confirmed

    def add(
        kind, side, neckline, first: Swing, last: Swing, height: float, lines: list, invalid=None
    ) -> bool:
        """Record the pattern unless it is old, or price closed beyond ``invalid`` before the break."""
        if end - last.i > PATTERN_RECENT_BARS:
            return False
        confirmed_at = last.i + SWING_BARS  # the pattern exists from the bar its last swing is known
        if confirmed_at > end:
            return False
        broke = _first_close_through(close, confirmed_at - 1, end, neckline, side) if side else None
        if broke is not None and end - broke > CONFIRMED_RECENT_BARS:
            return False
        if invalid is not None:  # e.g. a double top whose highs were taken out before the neckline broke
            beyond = _first_close_through(close, confirmed_at - 1, end, lambda i: invalid, -side)
            if beyond is not None and (broke is None or beyond < broke):
                return False
        level = neckline(broke if broke is not None else end)
        found.append({
            "kind": kind, "name": NAMES[kind], "side": side,
            "status": "confirmed" if broke is not None else "forming",
            "neckline": float(level), "target": float(level + side * height) if side else None,
            "start": first.i, "end": last.i, "broke": broke, "lines": lines,
            "record": RECORD["triangle" if kind.endswith("triangle") else kind],
        })  # fmt: skip
        return True

    # A pattern ends with its last swing; one more swing may have formed since (often the move that
    # broke the neckline), so the newest swings and the ones just before them are both looked at.
    for back in (0, 1):
        seq = points[: len(points) - back]
        if len(seq) < 3:
            break
        a, mid, b = seq[-3:]  # double top / bottom: two equal highs (lows), the neckline between
        if a.kind == b.kind != mid.kind and abs(a.price - b.price) <= equal:
            top = a.kind == "high"
            neck, extreme = mid.price, (max if top else min)(a.price, b.price)
            if add("double_top" if top else "double_bottom", -1 if top else 1, lambda i, n=neck: n, a, b,
                   abs(extreme - neck), [[mid.i, neck, end, neck]], invalid=extreme):  # fmt: skip
                break
    for back in (0, 1):
        seq = points[: len(points) - back]
        if len(seq) < 5:
            break
        p1, t1, p2, t2, p3 = seq[-5:]  # head and shoulders: shoulder, trough, head, trough, shoulder
        up = p1.kind == "high"
        head_out = (
            p2.price > max(p1.price, p3.price) + equal if up else p2.price < min(p1.price, p3.price) - equal
        )
        if head_out and abs(p1.price - p3.price) <= 2 * equal:
            neckline = lambda i, a=t1, b=t2: _line(a, b, i)  # noqa: E731
            if add("head_shoulders" if up else "inverse_head_shoulders", -1 if up else 1, neckline, p1, p3,
                   abs(p2.price - _line(t1, t2, p2.i)), [[t1.i, t1.price, end, _line(t1, t2, end)]],
                   invalid=p2.price):  # fmt: skip
                break
    # triangles: the newest two highs and two lows (swings alternate, so they are the last four)
    if len(points) >= 4:
        h1, h2 = (s for s in points[-4:] if s.kind == "high")
        l1, l2 = (s for s in points[-4:] if s.kind == "low")
        highs_flat, lows_flat = abs(h2.price - h1.price) <= equal, abs(l2.price - l1.price) <= equal
        falling, rising = h2.price < h1.price - equal, l2.price > l1.price + equal
        kind = (
            "symmetrical_triangle" if falling and rising
            else "ascending_triangle" if highs_flat and rising
            else "descending_triangle" if falling and lows_flat
            else None
        )  # fmt: skip
        if kind:
            upper = lambda i, a=h1, b=h2: _line(a, b, i)  # noqa: E731
            lower = lambda i, a=l1, b=l2: _line(a, b, i)  # noqa: E731
            up_break = _first_close_through(close, known_at - 1, end, upper, 1)
            down_break = _first_close_through(close, known_at - 1, end, lower, -1)
            breaks = sorted((i, s) for i, s in ((up_break, 1), (down_break, -1)) if i is not None)
            if breaks or upper(end) > lower(end):  # broken, or the lines have not met yet
                lean = {"ascending_triangle": 1, "descending_triangle": -1}.get(kind, 0)
                side = breaks[0][1] if breaks else lean  # symmetrical: no direction until it breaks
                lines = [[h1.i, h1.price, end, upper(end)], [l1.i, l1.price, end, lower(end)]]
                if side:
                    add(
                        kind,
                        side,
                        upper if side > 0 else lower,
                        points[-4],
                        points[-1],
                        h1.price - l1.price,
                        lines,
                    )
                elif end - points[-1].i <= PATTERN_RECENT_BARS:
                    found.append({
                        "kind": kind, "name": NAMES[kind], "side": 0, "status": "forming",
                        "neckline": float(upper(end)), "target": None, "start": points[-4].i,
                        "end": points[-1].i, "broke": None, "lines": lines, "record": RECORD["triangle"],
                    })  # fmt: skip
    return found


def describe(features: pd.DataFrame, digits: int) -> dict[str, Any]:
    """Structure, zones, round numbers and patterns as of the last closed bar, for the dashboard."""
    if features.empty or len(features) < 2 * SWING_BARS + 1:
        return {"state": "unknown", "text": "Not enough bars to read the structure.", "swings": [], "zones": [],
                "round": {}, "patterns": []}  # fmt: skip
    points = swings(features)
    close = float(features["close"].iat[-1])
    atr = float(features["atr"].iat[-1])
    n = len(features)
    state = structure_state(points, close)
    fmt = f"{{:.{digits}f}}"
    s = state["state"]
    text = {
        "up": "Uptrend: higher highs and higher lows",
        "down": "Downtrend: lower highs and lower lows",
        "range": "Range: the highs and lows do not line up into a trend",
        "unknown": "Not enough swings yet to read the structure",
    }[s]
    if state["last_high"] is not None:
        text += f" (last swing high {fmt.format(state['last_high'])}, last swing low {fmt.format(state['last_low'])})"
    if state["broken"]:
        level = state["last_low"] if state["broken"] < 0 else state["last_high"]
        text += (
            f". Structure broken: a bar closed {'below the last higher low' if state['broken'] < 0 else 'above the last lower high'} "
            f"{fmt.format(level)}, the first sign this {'uptrend' if state['broken'] < 0 else 'downtrend'} has ended"
        )
    found_zones = zones(points, atr, close, since=max(0, n - ZONE_LOOKBACK))
    above = [z for z in found_zones if z["side"] == "resistance"][:2]
    below = [z for z in found_zones if z["side"] == "support"][:2]
    inside = [z for z in found_zones if z["side"] == "inside"][:1]
    return {
        "state": s,
        "broken": state["broken"],
        "text": text + ".",
        "swings": [asdict(p) for p in points[-SWINGS_SHOWN:]],
        "zones": inside + above + below,
        "round": round_levels(close),
        "round_record": RECORD["round_ahead"],
        "broken_record": RECORD["structure_break"] if state["broken"] else "",
        "atr": atr,
        "patterns": patterns(features, points),
    }
