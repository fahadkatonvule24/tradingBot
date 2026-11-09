"""Dependency-free inline-SVG charts for the backtest report (themeable via CSS variables,
native hover tooltips via <title>, every chart backed by a table in the report)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from markupsafe import Markup, escape


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    tooltip: str = ""


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1.0
    exponent = 10 ** math.floor(math.log10(raw))
    for multiplier in (1, 2, 2.5, 5, 10):
        if raw <= multiplier * exponent:
            return multiplier * exponent
    return 10 * exponent


def nice_range(lo: float, hi: float, count: int = 4) -> list[float]:
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    step = _nice_step((hi - lo) / count)
    start = math.floor(lo / step) * step
    ticks = [start]
    while ticks[-1] < hi - 1e-12:
        ticks.append(ticks[-1] + step)
    return ticks


def compact(value: float, money: bool = False) -> str:
    sign = "-" if value < 0 else ""
    v = abs(value)
    prefix = "$" if money else ""
    if v >= 1_000_000:
        return f"{sign}{prefix}{v / 1_000_000:.2f}M"
    if v >= 100_000:
        return f"{sign}{prefix}{v / 1_000:.0f}K"
    if v >= 100 or float(v).is_integer():  # in full below 100K: equity ticks ($10,250) stay distinct
        return f"{sign}{prefix}{v:,.0f}"
    return f"{sign}{prefix}{v:,.2f}"


def _svg(width: int, height: int, body: list[str], label: str) -> Markup:
    return Markup(
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}" '
        f'xmlns="http://www.w3.org/2000/svg">' + "".join(body) + "</svg>"
    )


def _downsample(series: pd.Series, points: int, how: str = "last") -> pd.Series:
    """At most ``points`` values; ``how="min"`` keeps each bucket's worst value (drawdowns)."""
    n = len(series)
    if n <= points:
        return series
    buckets = np.arange(n) * points // n
    values = series.groupby(buckets).agg(how)
    stamps = pd.Series(series.index).groupby(buckets).last()
    return pd.Series(values.to_numpy(), index=pd.DatetimeIndex(stamps.to_numpy()))


def _time_labels(index: pd.Index, slots: int = 7) -> list[int]:
    n = len(index)
    if n <= 1:
        return [0]
    return sorted({round(i * (n - 1) / (slots - 1)) for i in range(slots)})


def line(
    series: pd.Series,
    label: str,
    money: bool = True,
    width: int = 1040,
    height: int = 300,
    baseline: float | None = None,
    area: bool = False,
    alert: bool = False,
) -> Markup:
    """Time-series line (equity) or filled area (drawdown, values <= 0)."""
    s = _downsample(series.dropna(), 520, "min" if area else "last")
    if s.empty:
        return Markup("")
    left, right, top, bottom = 62, 14, 14, 30
    pw, ph = width - left - right, height - top - bottom
    lo, hi = float(s.min()), float(s.max())
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    ticks = nice_range(lo, hi)
    y0, y1 = ticks[0], ticks[-1]

    def x(i: int) -> float:
        return left + pw * i / max(1, len(s) - 1)

    def y(v: float) -> float:
        return top + ph - ph * (v - y0) / (y1 - y0)

    body = []
    for t in ticks:
        body.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        text = f"{t:g}%" if not money else compact(t, money=True)
        body.append(f'<text class="tick" x="{left - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{text}</text>')
    if baseline is not None:
        body.append(
            f'<line class="axis" x1="{left}" x2="{width - right}" y1="{y(baseline):.1f}" y2="{y(baseline):.1f}"/>'
        )

    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s.to_numpy()))
    cls = "alert" if alert else "series"
    if area:
        zero = y(baseline if baseline is not None else y0)
        body.append(
            f'<polygon class="area {cls}" points="{x(0):.1f},{zero:.1f} {points} {x(len(s) - 1):.1f},{zero:.1f}"/>'
        )
    body.append(f'<polyline class="line {cls}" points="{points}"/>')

    # hover targets: one invisible column per point, with the exact value
    step = pw / max(1, len(s) - 1)
    for i, (when, v) in enumerate(s.items()):
        value = f"{v:.2f}%" if not money else f"${v:,.2f}"
        body.append(
            f'<g class="hit"><title>{when:%d %b %Y %H:%M} · {value}</title>'
            f'<rect x="{x(i) - step / 2:.1f}" y="{top}" width="{step:.2f}" height="{ph}" fill="transparent"/>'
            f'<circle class="dot {cls}" cx="{x(i):.1f}" cy="{y(v):.1f}" r="4"/></g>'
        )
    for i in _time_labels(s.index):
        body.append(
            f'<text class="tick" x="{x(i):.1f}" y="{height - 8}" text-anchor="middle">{s.index[i]:%b %Y}</text>'
        )
    return _svg(width, height, body, label)


def diverging_bars(bars: Sequence[Bar], label: str, width: int = 500, money: bool = True) -> Markup:
    """Horizontal bars around zero: gains in the series color, losses in the alert color."""
    if not bars:
        return Markup("")
    row, thick, label_w = 30, 14, 92
    height = row * len(bars) + 8
    span = max(abs(b.value) for b in bars) or 1
    has_neg = any(b.value < 0 for b in bars)
    has_pos = any(b.value > 0 for b in bars) or not has_neg
    value_w = 66  # room for the value label beyond each bar's end
    plot = width - label_w - value_w * (has_neg + has_pos)
    if has_neg and has_pos:
        zero, scale = label_w + value_w + plot / 2, (plot / 2) / span
    elif has_neg:
        zero, scale = label_w + value_w + plot, plot / span
    else:
        zero, scale = label_w, plot / span
    body = []
    for i, b in enumerate(bars):
        y = 4 + i * row
        w = abs(b.value) * scale
        x0 = zero if b.value >= 0 else zero - w
        cls = "series" if b.value >= 0 else "alert"
        r = min(4, thick / 2, w)
        if b.value >= 0:
            path = f"M{x0:.1f},{y + 8}H{x0 + w - r:.1f}Q{x0 + w:.1f},{y + 8} {x0 + w:.1f},{y + 8 + r}V{y + 8 + thick - r}Q{x0 + w:.1f},{y + 8 + thick} {x0 + w - r:.1f},{y + 8 + thick}H{x0:.1f}Z"
            vx, anchor = x0 + w + 6, "start"
        else:
            path = f"M{zero:.1f},{y + 8}H{x0 + r:.1f}Q{x0:.1f},{y + 8} {x0:.1f},{y + 8 + r}V{y + 8 + thick - r}Q{x0:.1f},{y + 8 + thick} {x0 + r:.1f},{y + 8 + thick}H{zero:.1f}Z"
            vx, anchor = x0 - 6, "end"
        value = compact(b.value, money) if money else f"{b.value:+.2f}"
        body.append(
            f'<g class="hit"><title>{escape(b.tooltip or b.label + ": " + value)}</title>'
            f'<rect x="0" y="{y}" width="{width}" height="{row}" fill="transparent"/>'
            f'<text class="label" x="{label_w - 10}" y="{y + 19}" text-anchor="end">{escape(b.label)}</text>'
            + (f'<path class="{cls}" d="{path}"/>' if w > 0.5 else "")
            + f'<text class="value" x="{vx:.1f}" y="{y + 19}" text-anchor="{anchor}">{value}</text></g>'
        )
    body.append(f'<line class="axis" x1="{zero:.1f}" x2="{zero:.1f}" y1="2" y2="{height - 2}"/>')
    return _svg(width, height, body, label)


def histogram(
    values: Sequence[float], label: str, width: int = 500, height: int = 220, bin_width: float = 0.25
) -> Markup:
    """Distribution of trade R-multiples; bins below 0R in the alert color."""
    if not values:
        return Markup("")
    lo = math.floor(min(values) / bin_width) * bin_width
    hi = math.ceil(max(values) / bin_width) * bin_width + bin_width
    edges = [lo + i * bin_width for i in range(round((hi - lo) / bin_width) + 1)]
    counts = [0] * (len(edges) - 1)
    for v in values:
        counts[min(len(counts) - 1, int((v - lo) / bin_width))] += 1
    left, right, top, bottom = 40, 10, 10, 28
    pw, ph = width - left - right, height - top - bottom
    ticks = nice_range(0, max(counts))
    slot = pw / len(counts)
    bw = max(1.0, min(24.0, slot - 2))
    body = []
    for t in ticks:
        yy = top + ph - ph * t / ticks[-1]
        body.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        body.append(f'<text class="tick" x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end">{t:.0f}</text>')
    for i, c in enumerate(counts):
        h = ph * c / ticks[-1]
        xx = left + i * slot + (slot - bw) / 2
        cls = "alert" if edges[i + 1] <= 0 else "series"
        r = min(3, bw / 2, h)
        path = f"M{xx:.1f},{top + ph:.1f}V{top + ph - h + r:.1f}Q{xx:.1f},{top + ph - h:.1f} {xx + r:.1f},{top + ph - h:.1f}H{xx + bw - r:.1f}Q{xx + bw:.1f},{top + ph - h:.1f} {xx + bw:.1f},{top + ph - h + r:.1f}V{top + ph:.1f}Z"
        body.append(
            f'<g class="hit"><title>{edges[i]:+.2f}R to {edges[i + 1]:+.2f}R: {c} trades</title>'
            f'<rect x="{left + i * slot:.1f}" y="{top}" width="{slot:.1f}" height="{ph}" fill="transparent"/>'
            + (f'<path class="{cls}" d="{path}"/>' if c else "")
            + "</g>"
        )
    zero_x = left + pw * (0 - lo) / (hi - lo)
    body.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{top + ph}" y2="{top + ph}"/>')
    for v in (lo, 0.0, hi):
        xx = left + pw * (v - lo) / (hi - lo)
        body.append(f'<text class="tick" x="{xx:.1f}" y="{height - 8}" text-anchor="middle">{v:+.1f}R</text>')
    body.append(f'<line class="axis" x1="{zero_x:.1f}" x2="{zero_x:.1f}" y1="{top}" y2="{top + ph}"/>')
    return _svg(width, height, body, label)


def monthly_heatmap(table: pd.DataFrame, label: str) -> Markup:
    """Year x month returns on a diverging scale (blue = gain, red = loss, grey = flat)."""
    if table.empty:
        return Markup("")
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    cell_w, cell_h, label_w, top = 62, 34, 52, 24
    width = label_w + cell_w * 12 + 4
    height = top + cell_h * len(table) + 4
    span = max(1.0, float(table.abs().max().max()))
    body = [
        f'<text class="tick" x="{label_w + cell_w * i + cell_w / 2}" y="16" text-anchor="middle">{m}</text>'
        for i, m in enumerate(months)
    ]
    for r, (year, values) in enumerate(table.iterrows()):
        y = top + r * cell_h
        body.append(
            f'<text class="label" x="{label_w - 8}" y="{y + cell_h / 2 + 4}" text-anchor="end">{year}</text>'
        )
        for m in range(1, 13):
            v = values.get(m)
            x = label_w + (m - 1) * cell_w
            if v is None or pd.isna(v):
                continue
            step = min(4, int(abs(v) / span * 4.999))
            cls = "cell mid" if step == 0 else f"cell {'pos' if v > 0 else 'neg'}{step}"
            ink = "ink-light" if step >= 3 else "ink-dark"
            body.append(
                f'<g class="hit"><title>{months[m - 1]} {year}: {v:+.2f}%</title>'
                f'<rect class="{cls}" x="{x + 1}" y="{y + 1}" width="{cell_w - 2}" height="{cell_h - 2}" rx="4"/>'
                f'<text class="cellv {ink}" x="{x + cell_w / 2}" y="{y + cell_h / 2 + 4}" text-anchor="middle">{v:+.1f}%</text></g>'
            )
    return _svg(width, height, body, label)
