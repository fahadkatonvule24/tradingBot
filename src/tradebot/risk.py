"""Position sizing and portfolio risk arithmetic."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from tradebot.broker.base import Deal, Position, SymbolSpec
from tradebot.config import RiskParams


@dataclass(frozen=True)
class SizeDecision:
    volume: float
    risk_amount: float
    """Account-currency loss if the stop is hit at this volume."""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.volume > 0


def loss_per_lot(spec: SymbolSpec, price_distance: float, tick_value: float | None = None) -> float:
    value = tick_value if tick_value is not None else spec.tick_value
    return price_distance / spec.tick_size * value


def position_size(
    spec: SymbolSpec,
    equity: float,
    stop_distance: float,
    risk: RiskParams,
    tick_value: float | None = None,
) -> SizeDecision:
    """Volume that loses ``risk_percent`` of equity if the stop is hit.

    If that volume is below the broker minimum the trade is *skipped* (v23 silently traded the
    minimum lot, risking more than configured) unless ``allow_min_lot_overrisk`` is set.
    """
    if not math.isfinite(equity) or equity <= 0:
        return SizeDecision(0.0, 0.0, "equity must be positive and finite")
    if (
        any(not math.isfinite(v) or v <= 0 for v in (spec.volume_min, spec.volume_max, spec.volume_step))
        or spec.volume_min > spec.volume_max
    ):
        return SizeDecision(0.0, 0.0, "invalid broker volume limits")
    value = spec.tick_value if tick_value is None else tick_value
    if any(not math.isfinite(v) or v <= 0 for v in (stop_distance, spec.tick_size, value)):
        return SizeDecision(0.0, 0.0, "invalid tick value or stop distance")
    per_lot = loss_per_lot(spec, stop_distance, value)
    if not math.isfinite(per_lot):
        return SizeDecision(0.0, 0.0, "invalid per-lot risk")
    if risk.fixed_lot is not None:
        volume = spec.normalize_volume(risk.fixed_lot)
        if volume < spec.volume_min:
            return SizeDecision(0.0, 0.0, "fixed lot is below the broker minimum")
        return SizeDecision(volume, volume * per_lot)

    if not math.isfinite(risk.risk_percent) or risk.risk_percent <= 0:
        return SizeDecision(0.0, 0.0, "risk percent must be positive and finite")
    budget = equity * risk.risk_percent / 100
    volume = spec.normalize_volume(budget / per_lot)
    if volume < spec.volume_min:
        if not risk.allow_min_lot_overrisk:
            minimum_risk = spec.volume_min * per_lot / equity * 100
            return SizeDecision(
                0.0, 0.0, f"minimum lot would risk {minimum_risk:.2f}% (> {risk.risk_percent}%)"
            )
        volume = spec.volume_min
    return SizeDecision(volume, volume * per_lot)


def open_risk(positions: list[Position], specs: dict[str, SymbolSpec]) -> float:
    """Loss at current stops; infinity means an unstopped or unmeasurable position.

    A missing spec or stop must never free up the portfolio's risk budget.
    """
    total = 0.0
    for p in positions:
        spec = specs.get(p.symbol)
        if spec is None or not p.sl or any(not math.isfinite(v) for v in (p.sl, p.price_open, p.volume)):
            return math.inf
        if p.side not in (-1, 1) or p.volume <= 0:
            return math.inf
        adverse = (p.price_open - p.sl) * p.side  # positive while the stop is on the losing side
        if adverse > 0:
            if any(not math.isfinite(v) or v <= 0 for v in (spec.tick_size, spec.tick_value)):
                return math.inf
            total += loss_per_lot(spec, adverse) * p.volume
    return total


def drawdown_percent(deals: Iterable[Deal], balance: float) -> float:
    """How far ``balance`` is below its highest point since the first of ``deals``, in percent.

    The balance curve is rebuilt backwards from today's balance and the deals' net results, so it
    needs no stored state and survives restarts, like the daily and weekly loss limits.
    """
    ordered = sorted(deals, key=lambda d: d.time)
    level = peak = balance - sum(d.profit for d in ordered)
    for deal in ordered:
        level += deal.profit
        peak = max(peak, level)
    return (peak - balance) / peak * 100 if peak > 0 else 0.0


def tighter_stop(side: int, current: float, candidate: float) -> float:
    """Stops only ever move in the trade's favour."""
    if not current:
        return candidate
    return max(current, candidate) if side > 0 else min(current, candidate)
