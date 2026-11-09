"""Command-line interface.

tradebot check                          # connect to MT5, show account, symbols, trading gate
tradebot run [--once] [--live]          # trading loop (dry run unless every live gate passes)
tradebot fetch --bars 60000             # download history to data/ for backtests
tradebot backtest --data data           # event-driven backtest + HTML report
tradebot optimize --data data --folds 4 # walk-forward optimisation (out-of-sample report)
tradebot journal --days 7               # summarise the trade journal
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from tradebot import __version__
from tradebot.config import STRATEGIES, BotConfig, ConfigError, load_config

console = Console(highlight=False)
log = logging.getLogger("tradebot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradebot", description="MetaTrader 5 trading bot with a realistic backtester."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", default="config/config.json", help="config file (default: %(default)s)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="connect to MT5 and show account, symbols and the trading gate")

    run = sub.add_parser("run", help="run the trading loop")
    run.add_argument("--once", action="store_true", help="run one cycle and exit")
    run.add_argument(
        "--live", action="store_true", help="allow real orders (the config must also allow them)"
    )

    fetch = sub.add_parser("fetch", help="download history from MT5 to CSV")
    fetch.add_argument("--symbols", nargs="+", help="default: symbols from the config")
    fetch.add_argument("--timeframes", nargs="+", help="default: the config timeframe")
    fetch.add_argument("--bars", type=int, default=60_000)
    fetch.add_argument("--out", type=Path, default=Path("data"))

    for name, help_text in (
        ("backtest", "backtest the config on CSV history"),
        ("optimize", "walk-forward optimisation"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--data", type=Path, default=Path("data"), help="folder with SYMBOL_TF.csv files")
        p.add_argument("--symbols", nargs="+", help="default: symbols from the config")
        p.add_argument("--strategy", choices=STRATEGIES, help="override strategy.name")
        p.add_argument("--timeframe", type=str.upper, help="override the config timeframe (e.g. H1)")
        p.add_argument("--from", dest="start", type=pd.Timestamp, help="start date (UTC)")
        p.add_argument("--to", dest="end", type=pd.Timestamp, help="end date (UTC)")
        p.add_argument("-o", "--out", type=Path, default=Path("reports"), help="report folder")
        if name == "optimize":
            p.add_argument("--folds", type=int, default=4)
            p.add_argument(
                "--objective", default="sharpe", choices=["sharpe", "profit_factor", "return_pct", "calmar"]
            )
            p.add_argument("--min-trades", type=int, default=20)
            p.add_argument("--jobs", type=int, help="worker processes (default: CPUs - 1)")
            p.add_argument(
                "--write-config", type=Path, help="write the config with the latest tuned parameters here"
            )

    journal = sub.add_parser("journal", help="summarise the trade journal")
    journal.add_argument("--days", type=int, default=7)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.command in ("run",))
    try:
        config = load_config(args.config)
        if getattr(args, "symbols", None):
            # A command-line subset keeps only the overrides that still apply.
            keep = {k: v for k, v in config.overrides.items() if k in args.symbols}
            config = replace(config, symbols=tuple(args.symbols), overrides=keep)
        if getattr(args, "strategy", None):
            config = config.with_strategy(name=args.strategy)
        if getattr(args, "timeframe", None):
            config = replace(config, timeframe=args.timeframe)
        config.validate()
        handler = {
            "check": cmd_check, "run": cmd_run, "fetch": cmd_fetch, "backtest": cmd_backtest,
            "optimize": cmd_optimize, "journal": cmd_journal,
        }[args.command]  # fmt: skip
        return handler(config, args)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        console.print(f"[bold red]error:[/] {escape(str(exc))}")
        return 2


def _setup_logging(verbose: bool, to_file: bool) -> None:
    handlers: list[logging.Handler] = [RichHandler(console=console, show_path=False, markup=False)]
    if to_file:
        file_handler = RotatingFileHandler(
            "tradebot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handlers.append(file_handler)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, format="%(message)s", handlers=handlers, force=True
    )


# --- MT5 commands ---------------------------------------------------------------------------------


def _connect(config: BotConfig):
    from tradebot.broker.mt5 import MT5Broker

    broker = MT5Broker(config.mt5, config.server_utc_offset_hours)
    account = broker.connect()
    symbols = []
    for wanted in config.symbols:
        name = broker.resolve(wanted)
        if name is None:
            log.warning("symbol %s is not offered by this broker; skipping it", wanted)
        else:
            symbols.append(name)
    if not symbols:
        raise ValueError("none of the configured symbols are available on this account")
    return broker, account, symbols


def cmd_check(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.live import trading_gate

    broker, account, symbols = _connect(config)
    try:
        gate = trading_gate(
            config, account, live_flag=True, terminal_allows=broker.terminal_trading_allowed()
        )
        kind = "[bold red]REAL MONEY[/]" if account.is_real else "[green]demo[/]"
        console.print(Panel.fit(
            f"Server [bold]{escape(broker.server_name())}[/] · {kind} account · {account.currency} · leverage 1:{account.leverage}\n"
            f"Balance {account.balance:,.2f} · equity {account.equity:,.2f} · server time UTC{broker.offset.total_seconds() / 3600:+.2f}h\n"
            f"Orders with --live: {'[bold green]would be sent[/]' if gate.execute else '[yellow]blocked[/] (' + escape(gate.reason) + ')'}",
            title=f"tradebot {__version__}", border_style="grey50",
        ))  # fmt: skip
        table = Table(box=None, header_style="bold grey50", pad_edge=False)
        for col in ("Symbol", "Digits", "Spread", "Min lot", "Bars", "Last closed bar (UTC)", "Signal"):
            table.add_column(col, justify="right" if col in ("Digits", "Min lot") else "left")
        from tradebot.live import latest_features

        for symbol in symbols:
            spec = broker.symbol(symbol)
            features = latest_features(broker, config, symbol)
            row = features.iloc[-1]
            tick = broker.tick(symbol)
            signal = {1: "[green]BUY[/]", -1: "[red]SELL[/]"}.get(int(row["signal"]), "—")
            table.add_row(symbol, str(spec.digits), f"{(tick.ask - tick.bid) / spec.point:.0f} pts", str(spec.volume_min),
                          str(len(features)), f"{row['time']:%Y-%m-%d %H:%M}", signal)  # fmt: skip
        console.print(table)
    finally:
        broker.shutdown()
    return 0


def cmd_run(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.engine import Engine
    from tradebot.journal import Journal
    from tradebot.live import LiveRunner, trading_gate

    broker, account, symbols = _connect(config)
    journal = Journal(config.journal)
    try:
        gate = trading_gate(config, account, args.live, broker.terminal_trading_allowed())
        if gate.execute:
            log.warning(
                "LIVE TRADING on %s: orders WILL be sent. Create a file named %s to pause entries.",
                gate.reason,
                config.kill_switch_file,
            )
        else:
            log.info("DRY RUN: %s. Signals are logged and journalled; no orders are sent.", gate.reason)
        engine = Engine(config, broker, execute=gate.execute, journal=journal, live=True)
        LiveRunner(config, broker, engine, symbols).run(once=args.once)
    finally:
        journal.close()
        broker.shutdown()
    return 0


def cmd_fetch(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.broker.mt5 import fetch_history
    from tradebot.data import bars_path, save_spec

    broker, _, symbols = _connect(config)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        for configured, symbol in zip(args.symbols or config.symbols, symbols, strict=False):
            save_spec(args.out, _rename(broker.symbol(symbol), configured))
            for tf in args.timeframes or [config.timeframe]:
                bars = fetch_history(broker, symbol, tf, args.bars)
                path = bars_path(args.out, configured, tf)
                bars.to_csv(path, index=False)
                console.print(
                    f"{configured:<8} {tf:<4} {len(bars):>7,} bars  {bars['time'].iloc[0]:%Y-%m-%d} → {bars['time'].iloc[-1]:%Y-%m-%d}  [grey50]{path}[/]"
                )
    finally:
        broker.shutdown()
    return 0


def _rename(spec, name):
    return replace(spec, name=name)


# --- research commands ----------------------------------------------------------------------------


def _load(config: BotConfig, args: argparse.Namespace):
    from tradebot.data import load_dataset

    data, specs = load_dataset(args.data, list(config.symbols), config.timeframe)
    if args.start is not None or args.end is not None:
        start = args.start or pd.Timestamp.min
        end = args.end or pd.Timestamp.max
        data = {
            s: df[(df["time"] >= start) & (df["time"] < end)].reset_index(drop=True) for s, df in data.items()
        }
    return data, specs


def cmd_backtest(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.backtest import run_backtest
    from tradebot.report import render_backtest, write_report

    data, specs = _load(config, args)
    with console.status("Backtesting…"):
        result = run_backtest(config, data, specs)
    print_summary(
        result.metrics,
        result.per_symbol,
        f"Backtest · {config.strategy.name} · {config.timeframe}",
        result.start,
        result.end,
    )
    if result.skips:
        console.print(
            "[grey50]Signals skipped by guards:[/] "
            + ", ".join(f"{k} {v}" for k, v in sorted(result.skips.items(), key=lambda kv: -kv[1]))
        )
    report = write_report(
        render_backtest(result), args.out, f"backtest_{config.strategy.name}_{config.timeframe}.html"
    )
    trades_path = args.out / f"trades_{config.strategy.name}_{config.timeframe}.csv"
    _write_trades(result.trades, trades_path)
    console.print(f"\n[grey50]Report[/]  {report}\n[grey50]Trades[/]  {trades_path}")
    return 0


def cmd_optimize(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.backtest import run_backtest
    from tradebot.optimize import apply_params, walk_forward
    from tradebot.report import render_backtest, write_report

    data, specs = _load(config, args)
    with console.status(f"Walk-forward: {config.strategy.name}, {args.folds} folds…"):
        wf = walk_forward(
            config,
            data,
            specs,
            folds=args.folds,
            objective=args.objective,
            min_trades=args.min_trades,
            jobs=args.jobs,
        )
    table = Table(title="Walk-forward windows", box=None, header_style="bold grey50", title_justify="left")
    for col in (
        "Traded on (out-of-sample)",
        "Parameters chosen in-sample",
        "IS Sharpe",
        "OOS Sharpe",
        "OOS return",
        "OOS trades",
    ):
        table.add_column(col, justify="right" if "Sharpe" in col or "OOS" in col else "left")
    for f in wf.folds:
        params = ", ".join(f"{k.split('.')[1]}={v}" for k, v in f.params.items())
        style = "green" if f.out_of_sample.return_pct > 0 else "red"
        table.add_row(f"{f.test[0]:%Y-%m-%d} → {f.test[1]:%Y-%m-%d}", params, f"{f.in_sample.sharpe:.2f}",
                      f"{f.out_of_sample.sharpe:.2f}", f"[{style}]{f.out_of_sample.return_pct:+.2f}%[/]", str(f.out_of_sample.trades))  # fmt: skip
    console.print(table)
    print_summary(
        wf.oos_metrics,
        wf.oos_per_symbol,
        f"Out-of-sample (stitched) · {config.strategy.name}",
        wf.oos_equity.index[0],
        wf.oos_equity.index[-1],
    )
    efficiency = wf.efficiency
    console.print(
        f"Walk-forward efficiency: [bold]{efficiency:.2f}[/] [grey50](out-of-sample ÷ in-sample annual return; ≥ 0.5 is respectable, ≤ 0 means the in-sample edge was curve fitting)[/]"
        if efficiency == efficiency
        else "Walk-forward efficiency: n/a (in-sample return was not positive)"
    )

    reference = run_backtest(config, data, specs)
    report = write_report(
        render_backtest(reference, walk_forward=wf),
        args.out,
        f"walkforward_{config.strategy.name}_{config.timeframe}.html",
    )
    console.print(f"\n[grey50]Report[/]  {report}")
    if args.write_config:
        tuned = apply_params(config, wf.final_params)
        args.write_config.write_text(json.dumps(tuned.to_dict(), indent=2, default=str), encoding="utf-8")
        console.print(f"[grey50]Tuned config[/]  {args.write_config}  [yellow](review before trading it)[/]")
    return 0


def cmd_journal(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.journal import Journal

    if not Path(config.journal).exists():
        console.print(f"No journal yet at {config.journal}.")
        return 0
    journal = Journal(config.journal)
    rows = journal.summary(args.days, datetime.now(timezone.utc).replace(tzinfo=None))
    table = Table(
        title=f"Journal · last {args.days} days", box=None, header_style="bold grey50", title_justify="left"
    )
    for col in ("Symbol", "Event", "Count", "P/L"):
        table.add_column(col, justify="right" if col in ("Count", "P/L") else "left")
    for symbol, kind, count, pnl in rows:
        table.add_row(symbol, kind, str(count), f"{pnl:,.2f}")
    console.print(table if rows else "No events in that period.")
    journal.close()
    return 0


# --- output helpers -------------------------------------------------------------------------------


def print_summary(m, per_symbol: pd.DataFrame, title: str, start, end) -> None:
    color = "green" if m.return_pct > 0 else "red"
    console.print(Panel.fit(
        f"{start:%Y-%m-%d} → {end:%Y-%m-%d} ({m.years:.1f} y) · start {m.start_balance:,.0f}\n"
        f"Return [bold {color}]{m.return_pct:+.2f}%[/] (CAGR {m.cagr_pct:+.2f}%) · max drawdown [bold]{m.max_drawdown_pct:.2f}%[/] · "
        f"Sharpe [bold]{m.sharpe:.2f}[/] · profit factor [bold]{m.profit_factor:.2f}[/]\n"
        f"{m.trades} trades · {m.win_rate:.1f}% winners · expectancy {m.expectancy:+.2f} ({m.avg_r:+.2f}R) · longest losing streak {m.longest_losing_streak}",
        title=title, border_style="grey50",
    ))  # fmt: skip
    if per_symbol.empty:
        return
    table = Table(box=None, header_style="bold grey50", pad_edge=False)
    for col in ("Symbol", "Trades", "Win %", "Net", "PF", "Avg R"):
        table.add_column(col, justify="left" if col == "Symbol" else "right")
    for symbol, row in per_symbol.iterrows():
        net_color = "green" if row["net_profit"] > 0 else "red"
        table.add_row(str(symbol), f"{int(row['trades'])}", f"{row['win_rate']:.1f}", f"[{net_color}]{row['net_profit']:+,.2f}[/]",
                      f"{row['profit_factor']:.2f}", f"{row['avg_r']:+.2f}")  # fmt: skip
    console.print(table)


def _write_trades(trades, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [{**t.__dict__, "side": "buy" if t.side > 0 else "sell", "r_multiple": t.r_multiple} for t in trades]
    ).to_csv(path, index=False)


if __name__ == "__main__":
    sys.exit(main())
