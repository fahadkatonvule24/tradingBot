"""Position sizing and portfolio risk arithmetic."""

from __future__ import annotations

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
    per_lot = loss_per_lot(spec, stop_distance, tick_value)
    if per_lot <= 0:
        return SizeDecision(0.0, 0.0, "invalid tick value or stop distance")
    if risk.fixed_lot is not None:
        volume = spec.normalize_volume(risk.fixed_lot)
        return SizeDecision(volume, volume * per_lot)

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
    """Account-currency loss if every open position hit its *current* stop (0 once stops are past entry)."""
    total = 0.0
    for p in positions:
        spec = specs.get(p.symbol)
        if spec is None or not p.sl:
            continue
        adverse = (p.price_open - p.sl) * p.side  # positive while the stop is on the losing side
        if adverse > 0:
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
