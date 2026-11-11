"""Command-line interface.

tradebot check                          # connect to MT5, show account, symbols, trading gate
tradebot run [--once] [--live]          # trading loop (dry run unless every live gate passes)
tradebot fetch --bars 60000             # download history to data/ for backtests
tradebot backtest --data data           # event-driven backtest + HTML report
tradebot optimize --data data --folds 4 # walk-forward optimisation (out-of-sample report)
tradebot journal --days 7               # summarise the trade journal
tradebot plan [--out PLAN.md]           # the written trading plan, generated from the config
tradebot review --days 30               # closed trades in R: the bot's signals and your own calls
"""

from __future__ import annotations

import argparse
import codecs
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
    run.add_argument("--timeframe", type=str.upper, help="override the config timeframe (e.g. M30)")
    run.add_argument("--strategy", choices=STRATEGIES, help="override strategy.name")
    run.add_argument(
        "--live", action="store_true", help="allow real orders (the config must also allow them)"
    )

    ui = sub.add_parser("ui", help="live advisor dashboard in the browser: when, where, and how much")
    ui.add_argument("--port", type=int, default=8765, help="local port (default: %(default)s)")
    ui.add_argument("--timeframe", type=str.upper, help="start on this trading timeframe (e.g. M30)")
    ui.add_argument("--strategy", choices=STRATEGIES, help="start on this setup (e.g. retest)")
    ui.add_argument(
        "--live", action="store_true", help="enable the order button (the config must also allow orders)"
    )
    ui.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ui.add_argument("--data", type=Path, default=Path("data"), help="history for the per-symbol track record")

    fetch = sub.add_parser("fetch", help="download history from MT5 to CSV")
    fetch.add_argument("--symbols", nargs="+", help="default: symbols from the config")
    fetch.add_argument(
        "--timeframes", nargs="+", help="default: the config timeframe and the trend filter's timeframe"
    )
    fetch.add_argument(
        "--timeframe", type=str.upper, help="fetch this trading timeframe and the ones its trend filter needs"
    )
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

    sub.add_parser("telegram", help="set up and test phone alerts through a Telegram bot")

    journal = sub.add_parser("journal", help="summarise the trade journal")
    journal.add_argument("--days", type=int, default=7)

    plan = sub.add_parser("plan", help="the written trading plan, generated from the config")
    plan.add_argument("--strategy", choices=STRATEGIES, help="override strategy.name")
    plan.add_argument("--timeframe", type=str.upper, help="override the config timeframe (e.g. H1)")
    plan.add_argument("--data", type=Path, default=Path("data"), help="history used to measure the rules")
    plan.add_argument("--no-backtest", action="store_true", help="skip measuring the rules on stored history")
    plan.add_argument("-o", "--out", type=Path, help="also write the plan to this Markdown file")

    review = sub.add_parser("review", help="closed trades in R: the bot's signals against your own calls")
    review.add_argument("--days", type=int, default=30)
    return parser


ASCII_FALLBACK = {"→": "->", "≥": ">=", "≤": "<=", "÷": "/", "—": "-", "·": "-", "…": "..."}


def _ascii_fallback(error: UnicodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeEncodeError):
        raise error
    chunk = error.object[error.start : error.end]
    return "".join(ASCII_FALLBACK.get(ch, "?") for ch in chunk), error.end


codecs.register_error("tradebot.ascii_fallback", _ascii_fallback)


def safe_streams() -> None:
    """Never crash on output that the stream's encoding cannot represent.

    On Windows with Python < 3.15, output redirected to a file or pipe (`tradebot backtest >
    out.txt`, Task Scheduler logs, CI) is encoded as cp1252, which has no '→'. Non-UTF-8
    stdout/stderr get an error handler that prints readable ASCII ('->', '>=') instead of raising.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding and encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="tradebot.ascii_fallback")


def main(argv: Sequence[str] | None = None) -> int:
    safe_streams()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.command in ("run", "ui"))
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
            "check": cmd_check, "run": cmd_run, "ui": cmd_ui, "fetch": cmd_fetch, "backtest": cmd_backtest,
            "optimize": cmd_optimize, "journal": cmd_journal, "telegram": cmd_telegram,
            "plan": cmd_plan, "review": cmd_review,
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
            f"Balance {account.balance:,.2f} · equity {account.equity:,.2f}\n"
            f"Server time {escape(broker.server_clock.describe(broker.now()))}\n"
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
        notifier = _notifier()
        try:
            LiveRunner(config, broker, engine, symbols, notifier=notifier).run(once=args.once)
        finally:
            notifier.close()
    finally:
        journal.close()
        broker.shutdown()
    return 0


def _notifier():
    from tradebot.notify import Notifier

    notifier = Notifier.from_env()
    log.info(
        "Telegram alerts: %s", "on" if notifier.enabled else "off (run `tradebot telegram` to set them up)"
    )
    return notifier


def cmd_telegram(config: BotConfig, args: argparse.Namespace) -> int:
    """Guide the setup one step at a time; the token is never printed."""
    import os

    from tradebot.notify import CHAT_ENV, TOKEN_ENV, TelegramError, call, discover_chats

    token, chat = os.environ.get(TOKEN_ENV), os.environ.get(CHAT_ENV)
    if not token:
        console.print(
            "[bold]Phone alerts are not set up yet.[/]\n"
            "1. In Telegram, message [bold]@BotFather[/], send /newbot and follow the prompts. It gives you a token.\n"
            f"2. Add this line to a file named .env in this folder:  {TOKEN_ENV}=<the token>\n"
            "3. Open your new bot in Telegram and send it any message (for example: hi).\n"
            "4. Run [bold]tradebot telegram[/] again."
        )
        return 1
    try:
        if not chat:
            chats = discover_chats(token)
            if not chats:
                console.print(
                    "The token works, but nobody has messaged the bot yet. Send it any message in Telegram, then run this again."
                )
                return 1
            console.print("The token works. Add one of these to .env, then run this again:")
            for chat_id, name in chats:
                console.print(f"  {CHAT_ENV}={chat_id}    [grey50]# {escape(name)}[/]")
            return 1
        call(
            token,
            "sendMessage",
            {"chat_id": chat, "text": f"tradebot {__version__}: test message. Phone alerts are set up."},
        )
    except TelegramError as exc:
        console.print(f"[bold red]error:[/] {escape(str(exc))}")
        return 1
    console.print(
        "[green]Test message sent.[/] `tradebot ui` and `tradebot run` will now send alerts to this chat."
    )
    return 0


def cmd_ui(config: BotConfig, args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from tradebot.ui.monitor import Monitor
    from tradebot.ui.server import DashboardServer

    broker, _, symbols = _connect(config)
    notifier = _notifier()
    monitor = Monitor(
        config, broker, symbols, live_flag=args.live, journal_path=config.journal, notifier=notifier
    )
    names = {broker_name: wanted for wanted in config.symbols if (broker_name := broker.resolve(wanted))}
    worker = threading.Thread(target=monitor.run, name="mt5", daemon=True)
    try:
        server = DashboardServer(monitor, port=args.port)
    except OSError as exc:
        broker.shutdown()
        raise ValueError(f"cannot listen on port {args.port} ({exc}); try --port 8766") from None
    worker.start()
    threading.Thread(
        target=monitor.load_track_record, args=(args.data, names), name="track-record", daemon=True
    ).start()
    console.print(
        f"[bold]tradebot advisor[/] on [link={server.url}]{server.url}[/link]  "
        f"[grey50](orders {'enabled if the config allows' if args.live else 'off'}; Ctrl+C to stop)[/]"
    )
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        console.print("stopping…")
    finally:
        server.server_close()
        monitor.stop()
        worker.join(timeout=10)
        notifier.close()
        broker.shutdown()
    return 0


def cmd_fetch(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.broker.mt5 import fetch_history
    from tradebot.data import bars_path, save_spec, trend_timeframes

    broker, _, symbols = _connect(config)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        for configured, symbol in zip(args.symbols or config.symbols, symbols, strict=False):
            save_spec(args.out, _rename(broker.symbol(symbol), configured))
            trend = trend_timeframes(config).get(configured, ())
            for tf in args.timeframes or [config.timeframe, *trend]:
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
    from tradebot.data import load_dataset, load_trend_bars

    data, specs = load_dataset(args.data, list(config.symbols), config.timeframe)
    htf = load_trend_bars(args.data, config)  # full history: the trend EMA needs its warm-up
    if args.start is not None or args.end is not None:
        start = args.start or pd.Timestamp.min
        end = args.end or pd.Timestamp.max
        data = {
            s: df[(df["time"] >= start) & (df["time"] < end)].reset_index(drop=True) for s, df in data.items()
        }
    return data, specs, htf


def cmd_backtest(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.backtest import run_backtest
    from tradebot.report import render_backtest, write_report

    data, specs, htf = _load(config, args)
    with console.status("Backtesting…"):
        result = run_backtest(config, data, specs, htf=htf)
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
    tripped = _drawdown_trip(result.equity, config.risk.max_drawdown_percent, config.risk.drawdown_days)
    if tripped is not None:
        console.print(
            f"[grey50]Drawdown limit:[/] live trading would have paused entries on {tripped:%Y-%m-%d} "
            f"({config.risk.max_drawdown_percent:g}% below the {config.risk.drawdown_days}-day peak). "
            f"The backtest trades on, to show what the rules do."
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

    data, specs, htf = _load(config, args)
    with console.status(f"Walk-forward: {config.strategy.name}, {args.folds} folds…"):
        wf = walk_forward(
            config,
            data,
            specs,
            folds=args.folds,
            objective=args.objective,
            min_trades=args.min_trades,
            jobs=args.jobs,
            htf=htf,
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

    reference = run_backtest(config, data, specs, htf=htf)
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


def cmd_plan(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.plan import backtest_record, plan_title, to_markdown, trading_plan

    record = None
    if not args.no_backtest:
        from tradebot.backtest import run_backtest
        from tradebot.data import load_dataset, load_trend_bars

        try:
            data, specs = load_dataset(args.data, list(config.symbols), config.timeframe)
            with console.status("Measuring the rules on stored history…"):
                result = run_backtest(config, data, specs, htf=load_trend_bars(args.data, config))
            record = backtest_record(result)
        except (FileNotFoundError, ValueError) as exc:
            log.debug("plan: no backtest (%s)", exc)
    sections = trading_plan(config, record)
    console.print(f"[bold]{escape(plan_title(config))}[/]")
    for section in sections:
        console.print(f"\n[bold]{escape(section.title)}[/]")
        for line in section.lines:
            console.print(f"  - {escape(line)}")
    if args.out:
        args.out.write_text(to_markdown(config, sections), encoding="utf-8")
        console.print(f"\n[grey50]Written to[/] {args.out}")
    return 0


def cmd_review(config: BotConfig, args: argparse.Namespace) -> int:
    from datetime import timedelta

    from tradebot.review import closed_trades, summarize, verdict

    broker, _, _ = _connect(config)
    try:
        now = broker.now()
        since = now - timedelta(days=args.days)
        trades = closed_trades(broker, config.magic, since)
    finally:
        broker.shutdown()
    overall = summarize(trades)
    figures = f"{overall.trades} closed trades · {overall.win_rate:.1f}% winners · net {overall.net:+,.2f}\n"
    console.print(
        Panel.fit(
            f"{figures if trades else ''}{escape(verdict(overall))}",
            title=f"Review · last {args.days} days",
            border_style="grey50",
        )
    )
    if trades:
        table = Table(box=None, header_style="bold grey50", pad_edge=False)
        for col in ("", "Trades", "Win %", "Net", "PF", "Avg R", "Stopped", "Target", "Longest losing run"):
            table.add_column(col, justify="left" if col == "" else "right")
        groups = [
            ("The bot's signals", [t for t in trades if not t.manual]),
            ("Your own calls", [t for t in trades if t.manual]),
            *[
                (symbol, [t for t in trades if t.symbol == symbol])
                for symbol in sorted({t.symbol for t in trades})
            ],
        ]
        for label, group in groups:
            if not group:
                continue
            s = summarize(group)
            table.add_row(
                label, str(s.trades), f"{s.win_rate:.1f}", f"{s.net:+,.2f}",
                "n/a" if s.profit_factor is None else f"{s.profit_factor:.2f}",
                "n/a" if s.avg_r is None else f"{s.avg_r:+.2f}",
                str(s.stops), str(s.targets), str(s.longest_losing_streak),
            )  # fmt: skip
        console.print(table)
    dry = 0
    if Path(config.journal).exists():
        from tradebot.journal import Journal

        journal = Journal(config.journal)
        dry = sum(count for _, kind, count, _ in journal.summary(args.days, now) if kind == "DRY_OPEN")
        journal.close()
    if dry:
        console.print(
            f"[grey50]Dry run:[/] the bot logged {dry} signal{'' if dry == 1 else 's'} in this period without "
            f"sending an order. To see how they would have done: `tradebot fetch`, then "
            f"`tradebot backtest --from {since:%Y-%m-%d}`."
        )
    return 0


def _drawdown_trip(equity: pd.Series, limit_percent: float, days: int) -> pd.Timestamp | None:
    """The first moment equity was ``limit_percent`` below its highest point of the last ``days`` days."""
    if not limit_percent or equity.empty:
        return None
    peak = equity.rolling(f"{days}D").max()
    below = equity[equity <= peak * (1 - limit_percent / 100)]
    return below.index[0] if len(below) else None


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
