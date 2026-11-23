"""Position size, margin and leverage: a trading calculator on the terminal's own numbers.

Give it an instrument, a side and a stop (a price or a distance in pips), and either the lots or
how much to risk. It answers with the lot size, the money at the stop and at the target, the pip
value, what the position controls (its notional value) and the margin it needs at any leverage,
how much of the account that margin uses, how many lots the margin allows, and how far price can
move against the position before the margin level reaches the usual margin-call and stop-out
levels.

Money comes from the terminal's profit calculator at the exact prices, in the account currency.
Margin at a leverage you choose is notional / leverage, which is how brokers margin forex and
leveraged CFDs. Some brokers set gold, indices or crypto at a lower leverage than forex, or a
fixed margin per lot: give that instrument's leverage, or compare with the terminal's own figure,
which is shown alongside whenever the terminal has one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from tradebot.advisor import pip_size
from tradebot.broker.base import Broker, SymbolSpec

LEVERAGES = (10, 20, 30, 50, 100, 200, 300, 400, 500, 1000, 2000)
UNLIMITED = 1_000_000
"""Exness and some other brokers offer "unlimited" leverage; MT5 then reports a leverage of about
two billion. From this value up it is shown as unlimited, and only the terminal's margin counts."""


def leverage_text(leverage: float) -> str:
    return "unlimited" if leverage >= UNLIMITED else f"{leverage:g}"


MARGIN_CALL, STOP_OUT = 100.0, 50.0
"""Margin levels (equity / used margin, %) at which brokers commonly warn and then start closing
positions. Brokers differ (stop-out at 20% or 30% is also common); the levels are labelled."""


class CalcError(ValueError):
    pass


@dataclass(frozen=True)
class _Money:
    """Account-currency value of a price move for a number of lots, from the terminal when it can."""

    broker: Any
    symbol: str
    spec: SymbolSpec

    def __call__(self, side: int, lots: float, entry: float, price: float) -> float:
        calc = getattr(self.broker, "calc_profit", None)
        if calc is not None:
            result = calc(self.symbol, side, lots, entry, price)
            if result is not None and math.isfinite(result) and (result != 0 or entry == price):
                return float(result)
        return (price - entry) * side / self.spec.tick_size * self.spec.tick_value * lots


def _floor_lots(spec: SymbolSpec, lots: float) -> float:
    return spec.normalize_volume(max(lots, 0.0))


def _most_lots(spec: SymbolSpec, room: float, per_lot: float, margin_for: Any = None) -> float:
    """The most lots whose margin fits in ``room``: from the margin per lot, then checked with the
    terminal (``margin_for``) and stepped down while its figure is higher (tiered margin)."""
    if not per_lot or per_lot <= 0 or not math.isfinite(per_lot):
        return 0.0
    lots = _floor_lots(spec, room / per_lot)
    if margin_for is None:
        return lots
    for _ in range(50):
        needed = margin_for(lots) if lots >= spec.volume_min else None
        if needed is None or needed <= room + 1e-9:
            break
        lots = _floor_lots(spec, lots - spec.volume_step)
    return lots


def calculate(
    broker: Broker,
    symbol: str,
    side: int,
    *,
    entry: float | None = None,
    stop: float | None = None,
    stop_pips: float | None = None,
    target: float | None = None,
    target_pips: float | None = None,
    lots: float | None = None,
    risk_percent: float | None = None,
    risk_amount: float | None = None,
    equity: float | None = None,
    leverage: float | None = None,
    used_margin: float | None = None,
    margin_cap: float | None = None,
) -> dict[str, Any]:
    """Everything a trader works out before placing a trade, for one instrument and side."""
    if side not in (1, -1):
        raise CalcError("say buy or sell")
    spec, tick, account = broker.symbol(symbol), broker.tick(symbol), broker.account()
    money = _Money(broker, symbol, spec)
    pip = pip_size(spec)
    live = tick.ask if side > 0 else tick.bid
    entry = entry or live
    if stop is None and stop_pips:
        stop = spec.round_price(entry - side * stop_pips * pip)
    if target is None and target_pips:
        target = spec.round_price(entry + side * target_pips * pip)
    word = "buy" if side > 0 else "sell"
    if stop is not None and (stop - entry) * side >= 0:
        raise CalcError(f"the stop of a {word} must be {'below' if side > 0 else 'above'} the entry")
    if target is not None and (target - entry) * side <= 0:
        raise CalcError(f"the target of a {word} must be {'above' if side > 0 else 'below'} the entry")
    equity = account.equity if equity is None else equity
    if not equity or equity <= 0:
        raise CalcError("the equity must be positive")
    leverage = leverage or account.leverage
    if not leverage or leverage < 1:
        raise CalcError("the leverage is the N of 1:N, 1 or more")
    used = account.margin if used_margin is None else used_margin
    warnings: list[str] = []

    pip_value = money(side, 1.0, entry, entry + side * pip)
    loss_per_lot = -money(side, 1.0, entry, stop) if stop is not None else None
    if lots is not None:
        if lots <= 0:
            raise CalcError("the lots must be positive")
        sized = _floor_lots(spec, lots)
        if sized + 1e-12 < lots:
            warnings.append(
                f"{lots:g} lots is not a multiple of the lot step {spec.volume_step:g}: {sized:g} is."
            )
        lots, how = sized, "as given"
    elif loss_per_lot:
        budget = risk_amount if risk_amount is not None else equity * (risk_percent or 0) / 100
        if budget <= 0:
            raise CalcError("give the lots, or how much to risk")
        lots, how = _floor_lots(spec, budget / loss_per_lot), "from the risk"
        if lots < spec.volume_min:
            least = spec.volume_min * loss_per_lot
            warnings.append(
                f"The smallest lot, {spec.volume_min:g}, would risk {least:,.2f} ({least / equity * 100:.2f}% of "
                f"equity) at this stop: more than the {budget:,.2f} asked for. Skip the trade, or give the "
                f"lots to take the larger risk knowingly."
            )
            lots = 0.0
    else:
        raise CalcError("give a stop (price or pips) to size from risk, or give the lots")
    if lots > spec.volume_max:
        warnings.append(f"The broker's largest order is {spec.volume_max:g} lots.")

    per_lot_notional = entry / spec.tick_size * spec.tick_value
    notional = lots * per_lot_notional
    margin_per_lot = per_lot_notional / leverage
    terminal = broker.margin_required(symbol, side, lots) if lots else None
    same = int(leverage) == int(account.leverage) and used_margin is None  # this account, not a what-if
    exact = bool(terminal) and same
    margin = float(terminal) if terminal and exact else lots * margin_per_lot

    # The most lots use the same margin as the trade: the terminal's own (an instrument's own
    # leverage or a fixed margin per lot) on this account; notional / leverage for a what-if.
    def margin_for(n: float) -> float | None:
        if not same or n <= 0:
            return None
        value = broker.margin_required(symbol, side, n)
        return float(value) if value and math.isfinite(value) and value > 0 else None

    unit = margin_for(lots or spec.volume_min)
    per_lot_margin = unit / (lots or spec.volume_min) if unit else margin_per_lot
    total_margin = used + margin
    free_after = equity - total_margin
    level_after = equity / total_margin * 100 if total_margin > 0 else None
    cap = margin_cap or 0.0
    room = equity * cap / 100 - used
    max_by_cap = (
        _most_lots(spec, room, per_lot_margin, margin_for if unit else None) if cap and room > 0 else 0.0
    )
    max_by_free = (
        _most_lots(spec, equity - used, per_lot_margin, margin_for if unit else None)
        if equity > used
        else 0.0
    )
    if cap and lots and total_margin / equity * 100 > cap + 1e-9:
        warnings.append(
            f"Margin would reach {total_margin / equity * 100:.0f}% of equity, above your cap of {cap:g}%: "
            f"at most {max_by_cap:g} lots fit."
        )
    if lots and free_after < 0:
        warnings.append("There is not enough free margin for this position at this leverage.")

    def pips_until(level: float) -> float | None:
        """How far price can move against the position before the margin level reaches ``level``."""
        if not lots or not pip_value or total_margin <= 0:
            return None
        allowed = equity - level / 100 * total_margin
        return max(allowed, 0.0) / (abs(pip_value) * lots)

    risk = lots * loss_per_lot if loss_per_lot else None
    reward = money(side, lots, entry, target) if target is not None and lots else None
    held = broker.positions(None)
    open_notional = 0.0
    for p in held:
        try:
            other = broker.symbol(p.symbol)
            open_notional += p.volume * p.price_open / other.tick_size * other.tick_value
        except Exception:
            continue
    return {
        "symbol": symbol,
        "side": side,
        "digits": spec.digits,
        "pip": pip,
        "pip_label": "pips" if spec.digits in (3, 5) else "points",
        "currency": account.currency,
        "entry": entry,
        "live": entry == live,
        "stop": stop,
        "target": target,
        "stop_pips": abs(entry - stop) / pip if stop is not None else None,
        "target_pips": abs(target - entry) / pip if target is not None else None,
        "lots": lots,
        "lots_how": how,
        "volume_min": spec.volume_min,
        "volume_step": spec.volume_step,
        "pip_value_per_lot": pip_value,
        "pip_value": pip_value * lots,
        "risk_amount": risk,
        "risk_percent": risk / equity * 100 if risk is not None else None,
        "reward_amount": reward,
        "reward_risk": reward / risk if reward and risk else None,
        "equity": equity,
        "leverage": leverage,
        "account_leverage": account.leverage,
        "notional": notional,
        "margin": margin,
        "margin_method": "the terminal's own figure" if exact else f"notional / {leverage_text(leverage)}",
        "max_lots_method": "the terminal's margin" if unit else f"notional / {leverage_text(leverage)}",
        "leverage_label": leverage_text(leverage),
        "account_leverage_label": leverage_text(account.leverage),
        "terminal_margin": float(terminal) if terminal else None,
        "used_margin": used,
        "margin_percent": margin / equity * 100,
        "total_margin_percent": total_margin / equity * 100,
        "free_margin_after": free_after,
        "margin_level_after": level_after,
        "margin_cap": cap or None,
        "max_lots_by_cap": max_by_cap if cap else None,
        "max_lots_by_free_margin": max_by_free,
        "pips_to_margin_call": pips_until(MARGIN_CALL),
        "pips_to_stop_out": pips_until(STOP_OUT),
        "effective_leverage": (open_notional + notional) / equity,
        "open_positions": len(held),
        "by_leverage": [
            {
                "leverage": n,
                "margin": lots * per_lot_notional / n,
                "margin_percent": lots * per_lot_notional / n / equity * 100,
                "max_lots_by_cap": _floor_lots(spec, (equity * cap / 100 - used) / (per_lot_notional / n))
                if cap
                else None,
            }
            for n in LEVERAGES
        ],
        "warnings": warnings,
    }


def summary(r: dict[str, Any]) -> str:
    """The calculation in a few lines, for the phone."""
    d, cur, unit = r["digits"], r["currency"], r["pip_label"]
    word = "BUY" if r["side"] > 0 else "SELL"
    lines = [
        f"{word} {r['symbol']} at {r['entry']:.{d}f}{' (live)' if r['live'] else ''} · equity {r['equity']:,.2f} {cur} · 1:{leverage_text(r['leverage'])}"
    ]
    if r["stop"] is not None:
        lines.append(f"Stop {r['stop']:.{d}f} ({r['stop_pips']:.1f} {unit})"
                     + (f" · target {r['target']:.{d}f} ({r['target_pips']:.1f} {unit})" if r["target"] is not None else ""))  # fmt: skip
    if r["lots"]:
        lines.append(
            f"Size {r['lots']:g} lots ({r['lots_how']}) · {r['pip_value']:,.2f} {cur} per {unit[:-1]}"
        )
        if r["risk_amount"] is not None:
            reward = (
                f" · target pays {r['reward_amount']:,.2f} ({r['reward_risk']:.1f}R)"
                if r["reward_amount"]
                else ""
            )
            lines.append(
                f"At the stop: -{r['risk_amount']:,.2f} {cur} ({r['risk_percent']:.2f}% of equity){reward}"
            )
        lines.append(
            f"Controls {r['notional']:,.0f} {cur} · margin {r['margin']:,.2f} ({r['margin_percent']:.1f}% of equity; "
            f"{r['total_margin_percent']:.1f}% with what is open) · free after {r['free_margin_after']:,.2f}"
        )
        if r["pips_to_margin_call"] is not None:
            lines.append(f"Margin call (100%) after {r['pips_to_margin_call']:,.0f} {unit} against you, "
                         f"stop-out (50%) after {r['pips_to_stop_out']:,.0f}")  # fmt: skip
        lines.append(f"Account leverage in use: {r['effective_leverage']:.1f}x")
    cap = (
        f" · {r['max_lots_by_cap']:g} within your {r['margin_cap']:g}% margin cap" if r["margin_cap"] else ""
    )
    lines.append(f"Most lots: {r['max_lots_by_free_margin']:g} on all free margin{cap}")
    lines += r["warnings"]
    return "\n".join(lines)
