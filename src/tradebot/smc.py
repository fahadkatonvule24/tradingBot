"""Liquidity sweeps, fair value gaps and supply and demand zones, read from closed bars.

These are the "smart money" concepts traders use to say where stop orders rest and where price
left in a hurry. They are context for a person reading the chart, not signals: no independent study
has found an edge in them (an independent test of order blocks and fair value gaps on 738 assets
found none; sweep trades that won 72% of the time still trailed buy-and-hold), and the bot's own
measurement on the stored history is in ``RECORD``, shown next to each of them on the dashboard.
``scripts/research_smc.py`` repeats that measurement.

The common definitions, made exact so they can be measured:

Liquidity sweep: stop orders rest beyond swing points. A pivot high is a bar whose high is the
highest of the ``PIVOT_BARS`` bars either side of it; it becomes known ``PIVOT_BARS`` bars later
and is tracked for ``SWEEP_LOOKBACK`` bars or until a bar trades above it. A bar that trades above
one or more tracked pivot highs and closes back below all of them *swept* them: a bearish sweep.
Below pivot lows and closing back above them: a bullish sweep. A bar that closes beyond a level
broke it, and the level's liquidity is taken; if the next bar (within ``SWEEP_RECLAIM_BARS``)
closes back on the original side, that is a sweep too, dated on the reclaiming bar. Sources accept
one to three bars for the reclaim; two was measured (see ``RECORD``): with the same bar only, a
sweep against a join made no consistent difference; with two or three bars it did.

Fair value gap: three bars where the third's low is above the first's high (bullish: the gap runs
from the first high up to the third low) or the third's high is below the first's low (bearish).
Gaps under ``FVG_MIN_ATR`` ATR are noise. A gap stays open until a bar closes beyond its far edge
(filled, judged by close, the stricter of the two conventions); a bar trading into it tests it.

Supply and demand zone: one to ``ZONE_BASE_MAX`` small bars (body at most half the bar's range),
the base, then a departure bar whose body is at least ``ZONE_DEPART_ATR`` ATR and three fifths of
its range, closing beyond the whole base. Upwards: a demand zone from the base's lowest low (the
distal edge) to its highest body (the proximal edge). Downwards: a supply zone from the highest
high to the lowest body. The bar before the base names it: rally-base-rally and drop-base-rally
(demand), drop-base-drop and rally-base-drop (supply). A zone is fresh until price comes back to
its proximal edge, and broken by a close beyond its distal edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

PIVOT_BARS = 3
SWEEP_LOOKBACK = 100
"""A pivot older than this many bars is no longer watched for a sweep."""
SWEEP_RECLAIM_BARS = 2
"""Bars a sweep's reclaim may take: 1 = the same bar (see ``sweeps``)."""
SWEEP_RECENT_BARS = 3
"""A sweep this many bars old or newer counts as recent (for the cautions)."""
FVG_MIN_ATR = 0.2
FVG_LOOKBACK = 150
ZONE_BASE_MAX = 3
ZONE_BASE_BODY = 0.5
ZONE_DEPART_ATR = 1.0
ZONE_DEPART_BODY = 0.6
ZONE_MAX_HEIGHT_ATR = 1.5
"""A base wider than this is a range, not a zone."""
ZONE_LOOKBACK = 300
NEAR_ATR = 0.25
"""Price within this many ATR of a zone or gap edge is "at" it."""

RECORD = {
    "sweep": "As an entry, trading the reclaim lost 0.07R to 0.13R a trade in the stored history (40-44% won). "
    "Joining a trend within 3 bars of a sweep against it did worse in both periods (-0.08R to -0.09R a trade "
    "against -0.03R to -0.06R); signals taken on time were mixed.",
    "fvg": "As an entry, the first test of a gap lost 0.11R to 0.18R a trade in the stored history. Signals "
    "taken on time at an opposite gap did worse (-0.10R to -0.12R against -0.02R to -0.06R); joins no different.",
    "zone": "As an entry, the first touch of a fresh zone lost 0.04R to 0.10R a trade in the stored history. "
    "Joining a trend at an opposite zone did clearly worse in both periods (-0.13R to -0.15R against "
    "-0.03R to -0.06R), and so did signals on time (-0.07R to -0.10R against -0.04R to -0.07R).",
}
"""Measured by ``scripts/research_smc.py`` on 2026-11-18: six symbols, H1, 2017-2022 and 2023-2026
separately, all four setups' signals entered on time and 1-5 bars late, and each concept traded as
an entry through the engine with the bot's stops and costs. Two cautions held up for joins in both
periods, so they change advice: no join is offered at an opposite zone or within ``SWEEP_RECENT_BARS``
bars of a closed sweep against the trend. Every concept lost money as an entry of its own; every
setup's signals lose on average too, with or without them."""


@dataclass
class Gap:
    i: int
    """The third bar of the three: the gap is known when it closes."""
    side: int
    bottom: float
    top: float
    tested: bool = False
    filled_at: int | None = None


@dataclass
class Zone:
    i: int
    """The departure bar: the zone is known when it closes."""
    side: int
    """+1 demand, -1 supply."""
    kind: str
    distal: float
    proximal: float
    base_start: int
    touches: int = 0
    """Separate returns to the proximal edge: bars in a row that reach it are one return."""
    broken_at: int | None = None
    inside: bool = False
    """The last bar reached the zone (a return still under way)."""

    @property
    def low(self) -> float:
        return min(self.distal, self.proximal)

    @property
    def high(self) -> float:
        return max(self.distal, self.proximal)


@dataclass
class Scan:
    """Everything found in a frame, and per bar what it means for a trade (arrays, one per bar)."""

    sweep_side: np.ndarray
    sweep_level: np.ndarray
    sweep_extreme: np.ndarray
    fvg_trigger: np.ndarray
    zone_trigger: np.ndarray
    at_demand: np.ndarray
    at_supply: np.ndarray
    bull_gap_near: np.ndarray
    bear_gap_near: np.ndarray
    gaps: list[Gap] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)


def _arrays(df: pd.DataFrame) -> tuple[np.ndarray, ...]:
    return tuple(df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "atr"))


def sweeps(
    df: pd.DataFrame, k: int = PIVOT_BARS, lookback: int = SWEEP_LOOKBACK, reclaim: int | None = None
) -> pd.DataFrame:
    """Per bar: ``sweep_side`` (+1 lows swept and reclaimed, -1 highs swept and reclaimed, 0 none),
    the outermost level swept (``sweep_level``) and how far the wick went (``sweep_extreme``).

    ``reclaim`` (default ``SWEEP_RECLAIM_BARS``) is how many bars the reclaim may take: 1 is the
    same bar only; 2 also counts a bar that closed beyond the level followed by one closing back."""
    reclaim = SWEEP_RECLAIM_BARS if reclaim is None else reclaim
    _, high, low, close, _ = _arrays(df)
    n = len(df)
    side = np.zeros(n, dtype=np.int8)
    level = np.full(n, np.nan)
    extreme = np.full(n, np.nan)
    if n < 2 * k + 2:
        return pd.DataFrame(
            {"sweep_side": side, "sweep_level": level, "sweep_extreme": extreme}, index=df.index
        )
    window = 2 * k + 1
    top = pd.Series(high).rolling(window, center=True).max().to_numpy()
    bottom = pd.Series(low).rolling(window, center=True).min().to_numpy()
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    broke_highs: list[tuple[int, float, float]] = []  # (bar that closed above it, level, highest wick)
    broke_lows: list[tuple[int, float, float]] = []
    for i in range(n):
        j = i - 1 - k  # a pivot at j is known once bar j + k has closed, so bar i may use it
        if j >= k:
            if high[j] == top[j]:
                highs.append((j, high[j]))
            if low[j] == bottom[j]:
                lows.append((j, low[j]))
        highs = [(a, p) for a, p in highs if i - a <= lookback]
        lows = [(a, p) for a, p in lows if i - a <= lookback]
        broke_highs = [(b, p, max(w, high[i])) for b, p, w in broke_highs if i - b < reclaim]
        broke_lows = [(b, p, min(w, low[i])) for b, p, w in broke_lows if i - b < reclaim]
        above = [p for _, p in highs if high[i] > p]
        below = [p for _, p in lows if low[i] < p]
        bearish = bool(above) and close[i] < min(above)
        bullish = bool(below) and close[i] > max(below)
        back_down = [(p, w) for _, p, w in broke_highs if close[i] < p]  # broken earlier, closed back
        back_up = [(p, w) for _, p, w in broke_lows if close[i] > p]
        if bearish and not bullish:
            side[i], level[i], extreme[i] = -1, max(above), high[i]
        elif bullish and not bearish:
            side[i], level[i], extreme[i] = 1, min(below), low[i]
        elif back_down and not back_up:
            side[i], level[i], extreme[i] = -1, max(p for p, _ in back_down), max(w for _, w in back_down)
        elif back_up and not back_down:
            side[i], level[i], extreme[i] = 1, min(p for p, _ in back_up), min(w for _, w in back_up)
        if back_down:
            broke_highs = [x for x in broke_highs if close[i] >= x[1]]
        if back_up:
            broke_lows = [x for x in broke_lows if close[i] <= x[1]]
        if reclaim > 1:  # closed beyond: a break, unless a later bar within the window closes back
            broke_highs += [(i, p, high[i]) for p in above if close[i] >= p]
            broke_lows += [(i, p, low[i]) for p in below if close[i] <= p]
        if above:  # swept or broken, the orders there have been filled
            highs = [(a, p) for a, p in highs if high[i] <= p]
        if below:
            lows = [(a, p) for a, p in lows if low[i] >= p]
    return pd.DataFrame({"sweep_side": side, "sweep_level": level, "sweep_extreme": extreme}, index=df.index)


_CACHE: dict[tuple, Scan] = {}


def scan(df: pd.DataFrame) -> Scan:
    """Sweeps, gaps and zones bar by bar, using at each bar only what was known when it closed.
    The dashboard asks every second; closed bars change once a bar, so the answer is kept."""
    if len(df) == 0:
        return _scan(df)
    # the ATR too: the same bars with another ATR period (an optimisation grid) give other gaps and zones
    key = (len(df), str(df["time"].iat[0]) if "time" in df else 0, str(df["time"].iat[-1]) if "time" in df else 0,
           float(df["close"].iat[-1]), float(df["high"].iat[-1]), float(df["low"].iat[-1]),
           str(df["atr"].iat[-1]), str(df["atr"].iat[len(df) // 2]), SWEEP_RECLAIM_BARS)  # fmt: skip
    found = _CACHE.get(key)
    if found is None:
        if len(_CACHE) > 64:
            _CACHE.clear()
        found = _CACHE[key] = _scan(df)
    return found


def _scan(df: pd.DataFrame) -> Scan:
    open_, high, low, close, atr = _arrays(df)
    n = len(df)
    swept = sweeps(df)
    fvg_trigger = np.zeros(n, dtype=np.int8)
    zone_trigger = np.zeros(n, dtype=np.int8)
    at_demand = np.zeros(n, dtype=bool)
    at_supply = np.zeros(n, dtype=bool)
    bull_near = np.zeros(n, dtype=bool)
    bear_near = np.zeros(n, dtype=bool)
    gaps: list[Gap] = []
    zones: list[Zone] = []
    body = np.abs(close - open_)
    span = high - low
    for i in range(n):
        a = atr[i]
        ok = math.isfinite(a) and a > 0
        # 1. what this bar does to the gaps and zones known before it
        for g in gaps:
            if g.filled_at is not None:
                continue
            if g.side > 0:
                inside = low[i] <= g.top
                if close[i] < g.bottom:
                    g.filled_at = i
                elif inside and not g.tested:
                    g.tested = True
                    if close[i] > open_[i]:
                        fvg_trigger[i] = 1
            else:
                inside = high[i] >= g.bottom
                if close[i] > g.top:
                    g.filled_at = i
                elif inside and not g.tested:
                    g.tested = True
                    if close[i] < open_[i]:
                        fvg_trigger[i] = -1
        for z in zones:
            if z.broken_at is not None:
                continue
            if (close[i] - z.distal) * z.side < 0:  # closed beyond the far edge
                z.broken_at = i
                continue
            reached = low[i] <= z.proximal if z.side > 0 else high[i] >= z.proximal
            if reached and not z.inside:  # a new return (a bar in a row in the zone is the same one)
                if z.touches == 0 and (close[i] - z.proximal) * z.side > 0:  # first touch, closed back out
                    zone_trigger[i] = z.side
                z.touches += 1
            z.inside = reached
        gaps = [g for g in gaps if g.filled_at is None and i - g.i <= FVG_LOOKBACK]
        zones = [z for z in zones if z.broken_at is None and i - z.i <= ZONE_LOOKBACK]
        # 2. new ones that this bar completes
        if ok and i >= 2:
            if low[i] - high[i - 2] >= FVG_MIN_ATR * a:
                gaps.append(Gap(i, 1, high[i - 2], low[i]))
            elif low[i - 2] - high[i] >= FVG_MIN_ATR * a:
                gaps.append(Gap(i, -1, high[i], low[i - 2]))
        direction = 1 if close[i] > open_[i] else -1
        if ok and body[i] >= ZONE_DEPART_ATR * a and body[i] >= ZONE_DEPART_BODY * span[i]:
            for size in range(ZONE_BASE_MAX, 0, -1):  # the widest base of small bars right before it
                start = i - size
                if start < 1:
                    continue
                base = range(start, i)
                if not all(body[b] <= ZONE_BASE_BODY * span[b] for b in base):
                    continue
                base_high, base_low = max(high[b] for b in base), min(low[b] for b in base)
                if base_high - base_low > ZONE_MAX_HEIGHT_ATR * a:
                    continue
                if (direction > 0 and close[i] <= base_high) or (direction < 0 and close[i] >= base_low):
                    continue  # it did not leave the base
                tops = [max(open_[b], close[b]) for b in base]
                bottoms = [min(open_[b], close[b]) for b in base]
                into = 1 if close[start - 1] > open_[start - 1] else -1
                word = {1: "rally", -1: "drop"}
                kind = f"{word[into]}-base-{word[direction]}"
                if direction > 0:
                    zones.append(Zone(i, 1, kind, base_low, max(tops), start))
                else:
                    zones.append(Zone(i, -1, kind, base_high, min(bottoms), start))
                break
        # 3. where the close sits relative to what is open now
        if ok:
            c, near = close[i], NEAR_ATR * a
            for z in zones:
                if z.i == i:
                    continue  # price is leaving a zone on the bar that makes it
                if z.side > 0 and z.low - near <= c <= z.high + near:
                    at_demand[i] = True
                elif z.side < 0 and z.low - near <= c <= z.high + near:
                    at_supply[i] = True
            for g in gaps:
                if g.i == i:
                    continue
                if g.side > 0 and g.bottom - near <= c <= g.top + near:
                    bull_near[i] = True
                elif g.side < 0 and g.bottom - near <= c <= g.top + near:
                    bear_near[i] = True
    return Scan(
        swept["sweep_side"].to_numpy(), swept["sweep_level"].to_numpy(), swept["sweep_extreme"].to_numpy(),
        fvg_trigger, zone_trigger, at_demand, at_supply, bull_near, bear_near, gaps, zones,
    )  # fmt: skip


def against(found: Scan, i: int, side: int) -> list[str]:
    """Which cautions hold at bar ``i`` for a trade on ``side``: keys "sweep", "zone", "gap"."""
    out = []
    recent = found.sweep_side[max(0, i - SWEEP_RECENT_BARS + 1) : i + 1]
    if (recent == -side).any():
        out.append("sweep")
    if found.at_demand[i] if side < 0 else found.at_supply[i]:
        out.append("zone")
    if found.bull_gap_near[i] if side < 0 else found.bear_gap_near[i]:
        out.append("gap")
    return out


def _price(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


def cautions(features: pd.DataFrame, preview: pd.DataFrame | None, side: int, digits: int,
             bar_minutes: int) -> list[dict[str, Any]]:  # fmt: skip
    """What argues against a trade on ``side`` now, in words: a sweep the other way (on the bar
    still forming, or in the last few closed bars), or price at an opposite zone or gap."""
    if side not in (1, -1) or len(features) < 2 * PIVOT_BARS + 2:
        return []
    out: list[dict[str, Any]] = []
    other = "bullish" if side < 0 else "bearish"
    where = "low" if side < 0 else "high"
    back = "back above" if side < 0 else "back below"
    if preview is not None and len(preview) == len(features) + 1:
        forming = sweeps(preview.tail(SWEEP_LOOKBACK + 2 * PIVOT_BARS + 2)).iloc[-1]  # older pivots expired
        if int(forming["sweep_side"]) == -side:
            closes = pd.Timestamp(preview["time"].iat[-1]) + pd.Timedelta(minutes=bar_minutes)
            out.append({
                "key": "sweep", "forming": True,
                "text": f"The bar still forming went past the swing {where} {_price(forming['sweep_level'], digits)} "
                        f"and is {back} it now: a {other} sweep against this trade if it closes like this at "
                        f"{closes:%H:%M} UTC.",
            })  # fmt: skip
    found = scan(features)
    i = len(features) - 1
    recent = found.sweep_side[max(0, i - SWEEP_RECENT_BARS + 1) : i + 1]
    hits = np.flatnonzero(recent == -side)
    if hits.size:
        at = i - (len(recent) - 1 - int(hits[-1]))
        ago = i - at
        when = "on the last bar" if ago == 0 else f"{ago} bar{'s' if ago > 1 else ''} ago"
        out.append({
            "key": "sweep", "forming": False,
            "text": f"A {other} sweep {when}: price went past the swing {where} "
                    f"{_price(found.sweep_level[at], digits)} and closed {back} it.",
        })  # fmt: skip
    c = float(features["close"].iat[-1])
    near = NEAR_ATR * float(features["atr"].iat[-1])

    def price_at(lo: float, hi: float) -> bool:  # the same test as in ``scan``
        return lo - near <= c <= hi + near

    if (side < 0 and found.at_demand[i]) or (side > 0 and found.at_supply[i]):
        # the zone price is at, not merely the newest one (nor one made by the last bar, leaving it)
        z = next(
            (z for z in reversed(found.zones) if z.side == -side and z.i != i and price_at(z.low, z.high)),
            None,
        )
        if z is not None:
            name = "demand" if z.side > 0 else "supply"
            fresh = "a fresh" if z.touches == 0 else "a tested"
            out.append({
                "key": "zone", "forming": False,
                "text": f"Price is at {fresh} {name} zone {_price(z.low, digits)}-{_price(z.high, digits)} "
                        f"({z.kind}).",
            })  # fmt: skip
    if (side < 0 and found.bull_gap_near[i]) or (side > 0 and found.bear_gap_near[i]):
        g = next(
            (g for g in reversed(found.gaps) if g.side == -side and g.i != i and price_at(g.bottom, g.top)),
            None,
        )
        if g is not None:
            out.append({
                "key": "gap", "forming": False,
                "text": f"Price is at an open {'bullish' if g.side > 0 else 'bearish'} fair value gap "
                        f"{_price(g.bottom, digits)}-{_price(g.top, digits)}.",
            })  # fmt: skip
    return out


ZONES_SHOWN = 2
GAPS_SHOWN = 2
"""Drawn on the chart: what price is in, and this many nearest above it and below it. Every
live one drawn across the chart buried the candles in boxes far from the price."""


def nearest(items: list, price: float, bounds, each: int) -> list:
    """The items containing ``price``, then up to ``each`` nearest above it and below it, in their
    original order. ``bounds(item)`` gives (low, high)."""
    inside = [x for x in items if bounds(x)[0] <= price <= bounds(x)[1]]
    above = sorted((x for x in items if bounds(x)[0] > price), key=lambda x: bounds(x)[0])[:each]
    below = sorted((x for x in items if bounds(x)[1] < price), key=lambda x: -bounds(x)[1])[:each]
    keep = {id(x) for x in inside + above + below}
    return [x for x in items if id(x) in keep]


def describe(features: pd.DataFrame, offset: int = 0, shown_from: int = 0) -> dict[str, Any]:
    """Sweeps, open gaps and live zones for the chart, bar positions relative to ``offset`` (a gap
    or zone that began before the chart's first bar has a negative position). Sweeps before bar
    ``shown_from`` are left out; gaps and zones are shown while they are open."""
    if len(features) < 2 * PIVOT_BARS + 2:
        return {"sweeps": [], "gaps": [], "zones": [], "record": RECORD}
    found = scan(features)
    last = len(features) - 1
    sweeps_out = [
        {"i": int(i) - offset, "side": int(found.sweep_side[i]), "level": float(found.sweep_level[i]),
         "extreme": float(found.sweep_extreme[i])}
        for i in np.flatnonzero(found.sweep_side != 0) if i >= shown_from
    ]  # fmt: skip
    close = float(features["close"].iat[-1])
    gaps_out = [
        {"i": g.i - 2 - offset, "known": g.i - offset, "side": g.side, "bottom": g.bottom, "top": g.top,
         "status": "tested" if g.tested else "open"}
        for g in nearest(found.gaps, close, lambda g: (g.bottom, g.top), GAPS_SHOWN) if g.filled_at is None
    ]  # fmt: skip
    zones_out = [
        {"i": z.base_start - offset, "known": z.i - offset, "side": z.side, "kind": z.kind, "low": z.low,
         "high": z.high, "proximal": z.proximal, "distal": z.distal,
         "status": "fresh" if z.touches == 0 else f"tested {z.touches}x"}
        for z in nearest(found.zones, close, lambda z: (z.low, z.high), ZONES_SHOWN) if z.broken_at is None
    ]  # fmt: skip
    return {
        "sweeps": sweeps_out,
        "gaps": gaps_out,
        "zones": zones_out,
        "last": last - offset,
        "record": RECORD,
    }
