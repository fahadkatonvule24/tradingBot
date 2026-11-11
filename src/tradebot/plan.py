"""The trading plan, written out from the configuration.

A plan kept in a separate document drifts away from what the bot does. This one is generated from
the same settings the engine reads, so the written rules and the executed rules cannot differ:
what is traded, the exact entry conditions, the exits, the position size, the limits that stop
trading, when not to trade, how results are reviewed, and what the rules have measured so far.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradebot.config import BotConfig, StrategyParams
from tradebot.strategy import FILTERS

SETUP_LABELS = {
    "ema_cross": "EMA cross",
    "breakout": "Breakout",
    "pullback": "Pullback",
    "retest": "Break and retest",
}


@dataclass(frozen=True)
class Section:
    title: str
    lines: tuple[str, ...]


def _a(timeframe: str, capital: bool = False) -> str:
    """ "an H1", "an M15", "a D1"."""
    article = "an" if timeframe[:1] in "HM" else "a"
    return f"{article.capitalize() if capital else article} {timeframe}"


def _setup(p: StrategyParams, timeframe: str) -> str:
    if p.name == "ema_cross":
        return (
            f"EMA {p.fast_ema} closes across EMA {p.slow_ema} on {_a(timeframe)} bar: above it for a buy, "
            f"below it for a sell."
        )
    if p.name == "breakout":
        margin = f"at least {p.breakout_min_atr:g} ATR " if p.breakout_min_atr > 0 else ""
        return (
            f"{_a(timeframe, capital=True)} bar closes {margin}beyond the highest high (buy) or the lowest low (sell) of "
            f"the previous {p.breakout_period} bars. A wick through the level does not count."
        )
    if p.name == "pullback":
        return (
            f"RSI({p.pullback_rsi_period}) dips below {p.pullback_buy_below:g} and a bar closes with it back "
            f"above (buy), or it rises above {p.pullback_sell_above:g} and a bar closes with it back below "
            f"(sell): a pullback against the trend that has ended."
        )
    return (
        f"{_a(timeframe, capital=True)} bar closes through the previous {p.retest_period}-bar high or low. Within the next "
        f"{p.retest_window} bars price returns to within {p.retest_tolerance_atr:g} ATR of that level and a "
        f"bar closes away from it again. A close back through the level cancels the setup."
    )


def _filters(p: StrategyParams, timeframe: str) -> list[str]:
    applicable, lines = FILTERS[p.name], []
    if "trend" in applicable and p.use_trend_filter:
        ladder = p.trend_ladder(timeframe)
        if ladder:
            names = " and ".join(ladder)
            many = len(ladder) > 1
            lines.append(
                f"Direction: only with the higher-timeframe trend. The last completed {names} "
                f"bar{'s' if many else ''} must have closed above {'their' if many else 'its'} EMA "
                f"{p.trend_ema} for a buy, below for a sell"
                + (". If they disagree, there is no trade." if many else ".")
            )
    if "adx" in applicable and p.use_adx:
        lines.append(
            f"Trend strength: ADX({p.adx_period}) at least {p.adx_min:g}. Below that the market is "
            f"ranging and the setup is skipped."
        )
    if "macd" in applicable and p.use_macd:
        lines.append(
            f"Momentum: the MACD ({p.macd_fast}, {p.macd_slow}, {p.macd_signal}) histogram is positive "
            f"for a buy, negative for a sell."
        )
    if "volume" in applicable and p.use_volume_filter:
        lines.append(
            f"Volume: the bar that breaks out has at least {p.volume_min_ratio:g} times the average tick "
            f"volume of the previous {p.volume_period} bars."
        )
    if "rsi" in applicable:
        lines.append(
            f"Not overextended: RSI({p.rsi_period}) at most {p.rsi_buy_max:g} for a buy, at least "
            f"{p.rsi_sell_min:g} for a sell."
        )
    if "sr" in applicable and p.use_sr_filter:
        lines.append(
            f"Room to run: at least {p.sr_min_atr:g} ATR between price and the {p.sr_lookback}-bar high "
            f"(buy) or low (sell)."
        )
    if "fib" in applicable and p.use_fib_filter:
        lines.append(
            f"Near a 38%, 50% or 62% retracement of the last {p.fib_lookback} bars "
            f"(within {p.fib_tolerance_atr:g} ATR)."
        )
    return lines


def trading_plan(
    config: BotConfig, record: dict[str, Any] | None = None, measuring: bool = False
) -> list[Section]:
    """The plan for ``config``. ``record`` is a backtest summary of these rules, if one is available:
    start, end, trades, win_rate, profit_factor, avg_r and return_pct. ``measuring`` says that one
    is still being computed."""
    p, r, m, sched = config.strategy, config.risk, config.management, config.schedule
    tf = config.timeframe
    start, end = sched.hours()

    what = [
        f"Instruments: {', '.join(config.symbols)}.",
        f"Timeframe: {tf}. A decision is made once per bar, when it closes. A bar that is still forming "
        f"is never traded.",
        f"Hours: {start:02d}:00 to {end:02d}:00 UTC, "
        + ("every day." if sched.trade_weekends else "Monday to Friday."),
    ]
    changed = [
        f"{symbol}: {section}.{key} = {value}"
        for symbol, sections in config.overrides.items()
        for section, values in sections.items()
        if section != "backtest"
        for key, value in values.items()
    ]
    if changed:
        what.append("Per-instrument exceptions: " + "; ".join(changed) + ".")

    entry = [f"Setup ({SETUP_LABELS[p.name]}): {_setup(p, tf)}", *_filters(p, tf)]
    entry.append("All of these must be true on the same closed bar. If one is not, there is no trade.")

    exits = [
        f"Stop: {r.sl_atr:g} ATR from the entry price, sent with the order. Beyond it the setup is wrong.",
        f"Target: {r.tp_atr:g} ATR, so the reward is {r.tp_atr / r.sl_atr:.1f} times the risk (1R = the "
        f"distance to the stop).",
    ]
    if p.name == "breakout" and p.breakout_target == "measured":
        exits[1] = (
            f"Target: the measured move. The height of the {p.breakout_period}-bar range that broke is "
            f"projected from the broken level."
        )
    if r.min_reward_risk:
        exits.insert(
            2, f"A trade whose target is closer than {r.min_reward_risk:g} times its risk is skipped."
        )
    if m.partial_tp:
        exits.append(f"At +{m.partial_tp_r:g}R, {m.partial_tp_fraction:.0%} of the position is closed.")
    exits.append(
        f"At +{m.breakeven_r:g}R the stop moves to the entry price plus {m.breakeven_buffer_r:g}R, so the "
        f"trade can no longer lose."
    )
    exits.append(f"From +{m.trail_start_r:g}R the stop trails {m.trail_atr:g} ATR behind price.")
    if m.close_on_reverse:
        exits.append("An opposite signal closes the position.")
    if m.max_hold_bars:
        exits.append(f"A position still open after {m.max_hold_bars} bars is closed.")
    exits.append("The stop only ever moves in the trade's favour. It is never widened.")
    exits.append(
        f"Reversal warning, for any open position on a watched instrument, the bot's or one opened by hand. "
        f"Counted from the bar after the entry: an opposite signal that passes the filters; a {config.timeframe} "
        f"close beyond the prior {p.breakout_period}-bar extreme; EMA {p.fast_ema} crossing EMA {p.slow_ema} "
        f"against the trade; the higher-timeframe trend turning against it; the break it was riding failing. "
        f"One sign is a warning. The opposite signal, or two signs standing together, is the get-out level. "
        f"It is a warning to a person: the bot itself closes only on the opposite signal and its stops."
    )

    if r.fixed_lot is not None:
        size = [f"Every trade is {r.fixed_lot:g} lots (fixed)."]
    else:
        size = [
            f"Each trade risks {r.risk_percent:g}% of equity: volume = equity × {r.risk_percent:g}% ÷ the loss "
            f"per lot at the stop, rounded down to the broker's lot step.",
            "If the smallest lot the broker allows would risk more than that, the trade is "
            + ("taken at the smallest lot." if r.allow_min_lot_overrisk else "skipped."),
        ]

    limits = [
        f"Open risk: at most {r.max_total_risk_percent:g}% of equity across all open positions, counted "
        f"at their current stops.",
        f"Positions: at most {r.max_total_positions} in total and {r.max_positions_per_symbol} per "
        f"instrument"
        + (f"; margin in use at most {r.max_margin_percent:g}%." if r.max_margin_percent else "."),
    ]
    if r.daily_loss_limit_percent:
        limits.append(
            f"Day: a realised loss of {r.daily_loss_limit_percent:g}% stops entries until the next UTC day."
        )
    if r.weekly_loss_limit_percent:
        limits.append(
            f"Week: a realised loss of {r.weekly_loss_limit_percent:g}% stops entries until Monday."
        )
    if r.max_drawdown_percent:
        limits.append(
            f"Drawdown: {r.max_drawdown_percent:g}% below the highest balance of the last {r.drawdown_days} "
            f"days stops entries. (Live trading only; a backtest shows the full drawdown instead.)"
        )
    if r.max_consecutive_losses:
        limits.append(
            f"Losing streak: {r.max_consecutive_losses} losses in a row stop entries until the next UTC day."
        )
    if r.max_trades_per_day:
        limits.append(f"At most {r.max_trades_per_day} entries per day.")

    avoid = [f"Outside the trading hours above{'' if sched.trade_weekends else ', and at weekends'}."]
    if sched.news_file:
        avoid.append(
            f"From {sched.news_before_minutes} minutes before to {sched.news_after_minutes} minutes after a "
            f"scheduled release for either currency of the pair (listed in {sched.news_file})."
        )
    avoid += [
        f"When the spread is wider than {r.max_spread_atr:g} ATR.",
        f"When the last closed bar is more than {sched.max_bar_age_bars} bars old (market closed or data stale).",
        f"When a file named {config.kill_switch_file} exists in the working folder: the manual off switch.",
    ]

    review = [
        f"Every order, close and rejection is written to {config.journal}.",
        "`tradebot review` lists closed trades in R (multiples of the risk taken) and compares the bot's "
        "signals with trades placed by hand.",
        "Judge the rules on 50 to 100 trades, never on one. Change a rule only after testing it with "
        "`tradebot backtest` or `tradebot optimize`, not after a loss.",
    ]

    return [
        Section("What is traded, and when", tuple(what)),
        Section("Entry", tuple(entry)),
        Section("Exit", tuple(exits)),
        Section("Position size", tuple(size)),
        Section("Limits that stop new entries", tuple(limits)),
        Section("When not to trade", tuple(avoid)),
        Section("Review", tuple(review)),
        Section("What these rules have measured", tuple(_edge(config, record, measuring))),
    ]


def _edge(config: BotConfig, record: dict[str, Any] | None, measuring: bool = False) -> list[str]:
    lines = []
    if record and record.get("trades"):
        factor = record.get("profit_factor")
        lines.append(
            f"On stored history ({str(record['start'])[:7]} to {str(record['end'])[:7]}, "
            f"{record['trades']:,} trades) these rules averaged {record['avg_r']:+.2f}R per trade: "
            f"{record['win_rate']:.1f}% winners, profit factor "
            f"{'n/a' if factor is None else format(factor, '.2f')}, return {record['return_pct']:+.1f}%."
        )
        if record["avg_r"] <= 0:
            lines.append(
                "That is a negative expectancy. Risk management limits what it costs; it cannot make it "
                "positive. Keep this plan in dry run or on a demo account."
            )
        else:
            lines.append(
                "That figure is in-sample. Confirm it with `tradebot optimize` (walk-forward) and a forward "
                "test before risking money."
            )
    elif measuring:
        lines.append(
            "Being measured: the backtest of these rules on stored history is still running. Open the "
            "plan again in a minute."
        )
    else:
        lines.append(
            f"Not measured here: there is no stored {config.timeframe} history. Run `tradebot fetch` and "
            f"`tradebot backtest`. Until a backtest and a forward test both show a positive average trade, "
            f"keep this plan in dry run."
        )
    orders = "enabled in the config (the command line must also say --live)"
    if config.dry_run or not config.live_trading_confirm:
        orders = "off. This is a dry run: signals are logged and no order is sent"
    lines.append(
        f"Orders: {orders}. Real-money accounts are {'allowed' if config.allow_real_account else 'refused'}."
    )
    return lines


def backtest_record(result: Any) -> dict[str, Any]:
    """The summary ``trading_plan`` reads, from a ``BacktestResult``."""
    m = result.metrics
    return {
        "start": result.start,
        "end": result.end,
        "trades": m.trades,
        "return_pct": m.return_pct,
        "win_rate": m.win_rate,
        "profit_factor": m.profit_factor,
        "avg_r": m.avg_r,
    }


def plan_title(config: BotConfig) -> str:
    return f"Trading plan: {SETUP_LABELS[config.strategy.name]} on {config.timeframe}"


def to_markdown(config: BotConfig, sections: list[Section]) -> str:
    out = [
        f"# {plan_title(config)}",
        "",
        "Generated by `tradebot plan` from the configuration the bot runs on.",
        "",
    ]
    for section in sections:
        out += [f"## {section.title}", "", *[f"- {line}" for line in section.lines], ""]
    return "\n".join(out)
