"""Review of closed trades, in R.

The journal says what was done; this says how it turned out. Closed positions come from the
broker's own deal history (so trades closed by a stop or target at the broker are included), and
each result is divided by the risk taken at entry: the distance to the original stop, read back
from the order history. Trades placed by hand from the dashboard are reported separately from the
bot's signals, so a person can see whether their own calls did better or worse than the rules.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tradebot.broker.base import Broker
from tradebot.risk import loss_per_lot

ENOUGH_TRADES = 50
"""Below this, an average says more about luck than about the rules."""


@dataclass(frozen=True)
class ReviewedTrade:
    ticket: int
    symbol: str
    side: int
    opened: datetime
    closed: datetime
    pnl: float
    """Net of commission, swap and fees."""
    risk_amount: float | None
    """Account-currency loss the original stop stood for; None if the order had no stop."""
    manual: bool
    exit: str
    """stop (the original or a trailed stop), target, or other."""

    @property
    def r(self) -> float | None:
        return self.pnl / self.risk_amount if self.risk_amount else None


@dataclass(frozen=True)
class Summary:
    trades: int
    wins: int
    net: float
    profit_factor: float | None
    avg_r: float | None
    total_r: float
    measured: int
    """Trades whose risk is known, the ones ``avg_r`` covers."""
    longest_losing_streak: int
    stops: int
    targets: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0


def closed_trades(broker: Broker, magic: int, since: datetime) -> list[ReviewedTrade]:
    """Positions opened and fully closed since ``since``, oldest first."""
    by_position = defaultdict(list)
    for deal in broker.deals(since, magic):
        by_position[deal.position].append(deal)
    still_open = {p.ticket for p in broker.positions(magic)}
    opening = getattr(broker, "opening", None)
    out = []
    for ticket, deals in by_position.items():
        entries = [d for d in deals if d.entry == "in"]
        exits = [d for d in deals if d.entry == "out"]
        if ticket in still_open or not entries or not exits:
            continue  # still running, or opened before the window
        first, last = entries[0], exits[-1]
        stop = opening(ticket)[1] if opening else None
        risk = None
        if stop:
            spec = broker.symbol(first.symbol)
            risk = loss_per_lot(spec, abs(first.price - stop)) * sum(d.volume for d in entries) or None
        how = last.comment.lower()
        out.append(
            ReviewedTrade(
                ticket=ticket,
                symbol=first.symbol,
                side=first.side,
                opened=first.time,
                closed=last.time,
                pnl=sum(d.profit for d in deals),
                risk_amount=risk,
                manual="manual" in first.comment.lower(),
                exit="stop"
                if how.startswith(("[sl", "stop", "trailing stop"))
                else "target"
                if how.startswith(("[tp", "target"))
                else "other",
            )
        )
    return sorted(out, key=lambda t: t.closed)


def summarize(trades: Sequence[ReviewedTrade]) -> Summary:
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [-t.pnl for t in trades if t.pnl <= 0]
    rs = [t.r for t in trades if t.r is not None]
    streak = longest = 0
    for t in trades:
        streak = streak + 1 if t.pnl <= 0 else 0
        longest = max(longest, streak)
    return Summary(
        trades=len(trades),
        wins=len(wins),
        net=sum(t.pnl for t in trades),
        profit_factor=sum(wins) / sum(losses) if sum(losses) > 0 else None,
        avg_r=sum(rs) / len(rs) if rs else None,
        total_r=sum(rs),
        measured=len(rs),
        longest_losing_streak=longest,
        stops=sum(1 for t in trades if t.exit == "stop"),
        targets=sum(1 for t in trades if t.exit == "target"),
    )


def verdict(summary: Summary) -> str:
    """One plain sentence on what the sample does and does not show."""
    if not summary.trades:
        return "No closed trades in this period."
    if summary.avg_r is None:
        return "None of these trades had a stop on its opening order, so they cannot be measured in R."
    result = (
        f"{summary.avg_r:+.2f}R per trade over {summary.measured} trade{'' if summary.measured == 1 else 's'}"
    )
    if summary.measured < ENOUGH_TRADES:
        return (
            f"{result}. That is too few to judge the rules: at this size the average is mostly luck. "
            f"Keep the size small and review again at {ENOUGH_TRADES} to 100 trades."
        )
    if summary.avg_r > 0:
        return f"{result}: positive so far. Keep the size where it is until the next review confirms it."
    return f"{result}: the rules are losing. Do not raise the size; go back to testing."
