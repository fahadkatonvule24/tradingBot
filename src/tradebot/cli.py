"""Command-line interface.

tradebot check                          # connect to MT5, show account, symbols, trading gate
tradebot run [--once] [--live]          # trading loop (dry run unless every live gate passes)
tradebot fetch --bars 60000             # download history to data/ for backtests
tradebot backtest --data data           # event-driven backtest + HTML report
tradebot optimize --data data --folds 4 # walk-forward optimisation (out-of-sample report)
tradebot journal --days 7               # summarise the trade journal
tradebot plan [--out PLAN.md]           # the written trading plan, generated from the config
tradebot review --days 30               # closed trades in R: the bot's signals and your own calls
tradebot account [NAME EQUITY]          # your own accounts: the equity every size is worked out from
tradebot news [--days 2]                # the economic calendar for your instruments, pulled from the feed
"""

from __future__ import annotations

import argparse
import codecs
import json
import logging
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from tradebot import __version__
from tradebot.config import STRATEGIES, BotConfig, ConfigError, load_config
from tradebot.plan import setup_text

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
    ui.add_argument(
        "--port",
        type=int,
        help=f"local port (default: {DEFAULT_PORT}, or the next free one if another program has it)",
    )
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
    sub.add_parser("ntfy", help="set up and test phone alerts through the free ntfy app (send only)")
    whatsapp = sub.add_parser(
        "whatsapp", help="set up and test phone alerts on WhatsApp (send only, through CallMeBot)"
    )
    whatsapp.add_argument(
        "--sample", action="store_true", help="send a sample alert in the real format, marked as a sample"
    )

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
    review.add_argument("--account", help="review the trades you logged for this account of yours")

    news = sub.add_parser("news", help="download the week's economic calendar and list what is coming")
    news.add_argument("--days", type=int, default=2, help="how far ahead to list (default: %(default)s)")
    news.add_argument("--all", action="store_true", help="include low-impact events")

    account = sub.add_parser(
        "account", help="your own accounts: type in the equity and every size is worked out from it"
    )
    account.add_argument("name", nargs="?", help="account name, for example the broker's")
    account.add_argument("equity", nargs="?", type=float, help="its equity, in the terminal's currency")
    account.add_argument("--min-lot", type=float, help="smallest lot this broker accepts")
    account.add_argument("--lot-step", type=float, help="lot increment at this broker")
    account.add_argument("--leverage", type=float, help="the N of 1:N at this broker: margin is then checked")
    account.add_argument("--use", action="store_true", help="work everything out for this account")
    account.add_argument("--mt5", action="store_true", help="go back to the MetaTrader 5 account")
    account.add_argument("--remove", action="store_true", help="remove this account")
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
            config = config.with_setup(args.strategy)  # every symbol, per-symbol setups included
        if getattr(args, "timeframe", None):
            config = replace(config, timeframe=args.timeframe)
        config.validate()
        handler = {
            "check": cmd_check, "run": cmd_run, "ui": cmd_ui, "fetch": cmd_fetch, "backtest": cmd_backtest,
            "optimize": cmd_optimize, "journal": cmd_journal, "telegram": cmd_telegram, "whatsapp": cmd_whatsapp, "ntfy": cmd_ntfy,
            "plan": cmd_plan, "review": cmd_review, "account": cmd_account, "news": cmd_news,
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
    from tradebot.live import resolve_symbols

    broker = MT5Broker(config.mt5, config.server_utc_offset_hours)
    try:
        account = broker.connect()
        symbols = resolve_symbols(broker, config.symbols)
    except Exception:
        broker.shutdown()
        raise
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
            f"Server [bold]{escape(broker.server_name())}[/] · {kind} account · {account.currency} · leverage 1:{'unlimited' if account.leverage >= 1_000_000 else account.leverage}\n"
            f"Balance {account.balance:,.2f} · equity {account.equity:,.2f}\n"
            f"Server time {escape(broker.server_clock.describe(broker.now()))}\n"
            f"Orders with --live: {'[bold green]would be sent[/]' if gate.execute else '[yellow]blocked[/] (' + escape(gate.reason) + ')'}",
            title=f"tradebot {__version__}", border_style="grey50",
        ))  # fmt: skip
        table = Table(box=None, header_style="bold grey50", pad_edge=False)
        for col in (
            "Symbol",
            "Auto entry",
            "Digits",
            "Spread",
            "Min lot",
            "Bars",
            "Last closed bar (UTC)",
            "Signal",
        ):
            table.add_column(col, justify="right" if col in ("Digits", "Min lot") else "left")
        from tradebot.live import latest_features

        for symbol in symbols:
            mode = "enabled" if config.for_symbol(symbol).risk.auto_trade else "signals only"
            try:
                spec = broker.symbol(symbol)
                features = latest_features(broker, config, symbol)
                row = features.iloc[-1]
                tick = broker.tick(symbol)
                signal = {1: "[green]BUY[/]", -1: "[red]SELL[/]"}.get(int(row["signal"]), "—")
                table.add_row(symbol, mode, str(spec.digits), f"{(tick.ask - tick.bid) / spec.point:.0f} pts", str(spec.volume_min),
                              str(len(features)), f"{row['time']:%Y-%m-%d %H:%M}", signal)  # fmt: skip
            except Exception as exc:
                table.add_row(symbol, mode, "—", "—", "—", "—", "—", escape(f"unavailable: {exc}"))
        console.print(table)
    finally:
        broker.shutdown()
    return 0


def _working_folder(config: BotConfig) -> Path:
    """Where the journal is: the dashboard and the trading loop share their locks there."""
    return Path(config.journal).resolve().parent


def cmd_run(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.book import Book
    from tradebot.duty import Duty
    from tradebot.engine import Engine
    from tradebot.journal import Journal
    from tradebot.live import LiveRunner, trading_gate

    broker, account, symbols = _connect(config)
    with ExitStack() as resources:
        resources.callback(broker.shutdown)
        journal = Journal(config.journal)
        resources.callback(journal.close)
        gate = trading_gate(config, account, args.live, broker.terminal_trading_allowed())
        duty = Duty(_working_folder(config), dashboard=False, trading=gate.execute)
        resources.callback(duty.close)
        if gate.execute:
            log.warning(
                "LIVE TRADING on %s: orders WILL be sent. Create a file named %s to pause entries.",
                gate.reason,
                config.kill_switch_file,
            )
        else:
            log.info("DRY RUN: %s. Signals are logged and journalled; no orders are sent.", gate.reason)
        # with no dashboard running, this loop manages the trades placed by hand and sends the
        # reversal warnings, so restarting the dashboard leaves nothing unwatched (tradebot.duty)
        engine = Engine(config, broker, execute=gate.execute, journal=journal, live=True)
        automatic = [s for s in symbols if config.for_symbol(s).risk.auto_trade]
        signals_only = [s for s in symbols if not config.for_symbol(s).risk.auto_trade]
        log.info("Automatic entry symbols: %s", ", ".join(automatic) or "none")
        if signals_only:
            log.info("Signals-only symbols (risk.auto_trade=false): %s", ", ".join(signals_only))
        notifier = _notifier(config)
        resources.callback(notifier.close)
        book = Book(config.journal)
        resources.callback(book.close)
        feed = _news_feed(config)
        if feed:
            resources.callback(feed.stop)
            if args.once:
                _refresh_news(feed)  # one cycle: download now rather than in the background
        runner = LiveRunner(
            config, broker, engine, symbols, notifier=notifier, duty=duty, manage=gate.execute, book=book,
            gate=lambda: trading_gate(config, broker.account(), args.live, broker.terminal_trading_allowed()),
        )  # fmt: skip
        runner.run(once=args.once)
    return 0


def _notifier(config: BotConfig):
    from tradebot.notify import Alerts

    alerts = Alerts.from_env(whatsapp_level=config.alerts.whatsapp, ntfy_level=config.alerts.ntfy)
    log.info(
        "Phone alerts: %s",
        " and ".join(alerts.names)
        if alerts.enabled
        else "off (run `tradebot ntfy`, `tradebot whatsapp` or `tradebot telegram` to set them up)",
    )
    return alerts


def _news_feed(config: BotConfig, start: bool = True):
    """The week's calendar, kept up to date in the background, or None when the feed is off."""
    from tradebot.news import NewsFeed

    if not config.schedule.news_feed:
        return None
    feed = NewsFeed(config.schedule.news_feed, config.schedule.news_feed_file)
    if start:
        feed.start()
    return feed


def _refresh_news(feed) -> int | None:
    from tradebot.news import NewsError

    try:
        return feed.refresh()
    except NewsError as exc:
        log.warning("news calendar not updated: %s", exc)
        return None


def _currencies_of(symbols) -> set[str]:
    """EURUSD -> EUR and USD, XAUUSD -> XAU and USD, without asking a terminal."""
    out = set()
    for symbol in symbols:
        name = symbol.upper()
        if len(name) >= 6 and name[:6].isalpha():
            out |= {name[:3], name[3:6]}
    return out


def cmd_news(config: BotConfig, args: argparse.Namespace) -> int:
    from datetime import timedelta

    from tradebot.news import NewsCalendar

    schedule = config.schedule
    feed = _news_feed(config, start=False)
    if feed is not None:
        count = _refresh_news(feed)
        if count is not None:
            console.print(
                f"[grey50]Downloaded {count} events for this week into {schedule.news_feed_file}.[/]"
            )
    else:
        console.print(
            "[yellow]The news feed is off[/] (schedule.news_feed is empty): only your own file is used."
        )
    calendar = NewsCalendar(schedule.news_file, schedule.news_feed_file if schedule.news_feed else None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    currencies = _currencies_of(config.symbols)
    impacts = ("high", "medium", "low") if args.all else ("high", "medium")
    events = calendar.upcoming(now, currencies, hours=args.days * 24, impacts=impacts)
    if not events:
        console.print(f"No {'' if args.all else 'high- or medium-impact '}news for {', '.join(sorted(currencies))} "
                      f"in the next {args.days} day{'' if args.days == 1 else 's'}.")  # fmt: skip
        return 0
    table = Table(box=None, header_style="bold grey50", pad_edge=False)
    for col in ("When (UTC)", "In", "Currency", "Impact", "Event", "Forecast", "Previous"):
        table.add_column(col, justify="right" if col in ("In", "Forecast", "Previous") else "left")
    for e in events:
        left = e.time - now
        hours, minutes = divmod(int(left.total_seconds() // 60), 60)
        when = (
            "now"
            if abs(left) < timedelta(minutes=1)
            else ("passed" if left < timedelta(0) else f"{hours}h {minutes:02d}m")
        )
        style = "bold red" if e.impact in ("high", "red") else "yellow" if e.impact == "medium" else "grey50"
        table.add_row(f"{e.time:%a %d %b %H:%M}", when, e.currency, f"[{style}]{e.impact}[/]", escape(e.title),
                      escape(e.forecast), escape(e.previous))  # fmt: skip
    console.print(table)
    console.print(
        f"[grey50]New entries pause from {schedule.news_before_minutes} min before to {schedule.news_after_minutes} "
        f"min after each high-impact event for an instrument's currencies.[/]"
    )
    return 0


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


def cmd_ntfy(config: BotConfig, args: argparse.Namespace) -> int:
    """Make a private topic and save it to .env, then (next run) send a test notification."""
    import os
    import secrets

    from tradebot.notify import (
        NTFY_SERVER,
        NTFY_SERVER_ENV,
        NTFY_TOKEN_ENV,
        NTFY_TOPIC_ENV,
        NtfyError,
        ntfy_call,
        valid_topic,
    )

    topic = os.environ.get(NTFY_TOPIC_ENV)
    server = (os.environ.get(NTFY_SERVER_ENV) or NTFY_SERVER).rstrip("/")
    if not topic:
        topic = f"tradebot-{secrets.token_hex(6)}"
        env_file = Path(".env")
        existing = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
        lead = "" if not existing or existing.endswith("\n") else "\n"
        env_file.write_text(
            existing
            + lead
            + "# ntfy alerts. Anyone who knows the topic name can read them: keep it private.\n"
            f"{NTFY_TOPIC_ENV}={topic}\n",
            encoding="utf-8",
        )
        console.print(
            f"[bold]Your alert topic is[/]  [bold green]{topic}[/]  [grey50](saved to {env_file.resolve()})[/]\n"
            "1. Install the free [bold]ntfy[/] app on your phone (Google Play, F-Droid or the App Store).\n"
            f"2. In the app tap [bold]+[/], type the topic name above and tap Subscribe"
            f"{'' if server == NTFY_SERVER else f' (server: {server})'}.\n"
            "3. Run [bold]tradebot ntfy[/] again: it sends a test notification.\n"
            "[grey50]The topic name works like a password: anyone who knows it can read your alerts.[/]"
        )
        return 1
    if not valid_topic(topic):
        console.print(
            f"[bold red]error:[/] {NTFY_TOPIC_ENV} may hold only letters, digits, - and _ (64 at most)."
        )
        return 1
    try:
        ntfy_call(
            server,
            topic,
            "Test notification. Buy and sell signals, failed breaks, get-out warnings, news and orders "
            "will arrive here.",
            title=f"tradebot {__version__}",
            priority=4,
            token=os.environ.get(NTFY_TOKEN_ENV) or None,
        )
    except NtfyError as exc:
        console.print(f"[bold red]error:[/] {escape(str(exc))}")
        return 1
    level = config.alerts.ntfy
    console.print(
        f"[green]Sent[/] to the topic {topic}. If it reached your phone, `tradebot ui` and `tradebot run` will "
        f"send alerts there ({'all of them' if level == 'all' else 'the important ones'}: alerts.ntfy in the "
        "config). Restart a dashboard that is already running."
    )
    return 0


SAMPLE_ALERT = """SAMPLE ALERT · not a signal, do not trade it
This is the GBPUSD sell of 16 Nov 2026, 07:00 UTC, in the format real alerts use:

SELL · GBPUSD
Sell GBPUSD now. 20-bar low breakout · RSI 31.5 · ADX 29.8 · H4+D1 trend down. Valid until the H1 bar closes at 08:00 UTC.
SELL plan: entry ~1.31971 · SL 1.32140 · TP 1.31633 · 1.47 lots · risk 248.43 · target 496.86 · sized for MT5 MetaQuotes-Demo, equity 100,000 USD
Record: Breakout on GBPUSD H1 won 44% of 416 trades since 2016, average +0.05R. About 56 in 100 signals like this one lose, and nothing measured on the signal bar picks them out.

(That sell hit its stop at 08:40 UTC.)"""
"""What `tradebot whatsapp --sample` sends: a real alert from the activity log, with the size the
demo account showed and the setup's record on GBPUSD H1, labelled so it cannot pass for advice."""


CALLMEBOT_PAGE = "https://www.callmebot.com/blog/free-api-whatsapp-messages/"


def cmd_whatsapp(config: BotConfig, args: argparse.Namespace) -> int:
    """Guide the WhatsApp setup and send a test message to each number; keys are never printed."""
    import os

    from tradebot.notify import (
        PHONE_ENV,
        WHATSAPP_KEY_ENV,
        WHATSAPP_MAX_CHARS,
        WhatsAppError,
        chunks,
        masked,
        normalize_phone,
        whatsapp_call,
        whatsapp_recipients,
    )

    phone, key = os.environ.get(PHONE_ENV), os.environ.get(WHATSAPP_KEY_ENV)
    if not phone or not key:
        console.print(
            "[bold]WhatsApp alerts are not set up yet.[/] They go through CallMeBot, a free service for\n"
            "messages to your own number. It only sends: commands such as /plan need Telegram.\n"
            f"1. Follow the steps on {CALLMEBOT_PAGE}\n"
            "   (save their number as a contact and send it the activation message from WhatsApp).\n"
            "   It replies with your API key.\n"
            "2. Add these two lines to a file named .env in this folder:\n"
            f"     {PHONE_ENV}=+256700123456     [grey50]# your number, with the country code[/]\n"
            f"     {WHATSAPP_KEY_ENV}=<the key>\n"
            "3. Run [bold]tradebot whatsapp[/] again for a test message.\n"
            "[grey50]The alert text passes through CallMeBot's server on its way to you.[/]"
        )
        return 1
    pairs = whatsapp_recipients(dict(os.environ))
    if not pairs:
        console.print(
            f"[bold red]error:[/] {PHONE_ENV} and {WHATSAPP_KEY_ENV} need the same number of entries: "
            "one key per number, comma-separated, in the same order."
        )
        return 1
    failed = 0
    for raw, key in pairs:
        number = normalize_phone(raw)
        if not number:
            console.print(
                f"[bold red]error:[/] {escape(raw)} in {PHONE_ENV} needs the full international number, "
                "starting with + (for example +256700123456)."
            )
            failed += 1
            continue
        text = (
            SAMPLE_ALERT
            if getattr(args, "sample", False)
            else (f"tradebot {__version__}: test message. WhatsApp alerts are set up.")
        )
        try:
            replies = [whatsapp_call(number, key, part) for part in chunks(text, WHATSAPP_MAX_CHARS)]
        except WhatsAppError as exc:
            console.print(f"[bold red]error:[/] {masked(number)}: {escape(str(exc))}")
            failed += 1
            continue
        said = escape(" | ".join(r for r in replies if r)[:300].replace(number[1:], masked(number)[1:]))
        what = "a sample alert" if getattr(args, "sample", False) else "a test message"
        if all("queued" in (r or "").lower() for r in replies):
            console.print(
                f"[green]CallMeBot queued[/] {what} for {masked(number)}. [grey50]Its reply: {said}[/]"
            )
        else:
            console.print(
                f"[bold red]error:[/] CallMeBot did not say it queued {what} for {masked(number)}. "
                f"Its reply: {said or '(empty)'}"
            )
            failed += 1
    if failed:
        return 1
    level = config.alerts.whatsapp
    console.print(
        "Queued is not delivered: the message comes from CallMeBot's own number, in the chat where it sent "
        "your API key, usually within a minute.\n"
        f"If the test {'message reaches' if len(pairs) == 1 else 'messages reach'} WhatsApp, `tradebot ui` and "
        f"`tradebot run` will send alerts there ({'all of them' if level == 'all' else 'the important ones'}: "
        "alerts.whatsapp in the config); restart a dashboard that is already running. If nothing arrives "
        "within a few minutes, check the key."
    )
    return 0


DEFAULT_PORT = 8765
PORT_TRIES = 10


def open_dashboard(monitor: Any, port: int | None, factory: Any = None) -> Any:
    """Bind the dashboard server. A port given with --port is used or refused. Without one, the
    first free port from 8765 up is taken, because other programs do sit on 8765: seen with an
    antivirus service that had picked it for an outgoing connection."""
    if factory is None:
        from tradebot.ui.server import DashboardServer as factory

    candidates = [port] if port is not None else list(range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES))
    first: OSError | None = None
    for candidate in candidates:
        try:
            server = factory(monitor, port=candidate)
        except OSError as exc:
            first = first or exc
            continue
        if candidate != candidates[0]:
            console.print(
                f"[yellow]Port {candidates[0]} is taken by another program; using {candidate} instead.[/] "
                f"The address below is the one to open."
            )
        return server
    if port is not None:
        raise ValueError(f"cannot listen on port {port} ({first}); leave --port out to pick a free one")
    raise ValueError(
        f"cannot listen on any port from {candidates[0]} to {candidates[-1]} ({first}); "
        f"choose one with --port"
    )


def cmd_ui(config: BotConfig, args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from tradebot.book import Book
    from tradebot.duty import Duty
    from tradebot.ui.monitor import Monitor

    broker, _, symbols = _connect(config)
    notifier = _notifier(config)
    book = Book(config.journal)
    duty = Duty(_working_folder(config), dashboard=True)
    if not duty.held() and not duty.primary:
        console.print(
            "[yellow]Another tradebot dashboard is already running: this one sends nothing to the phone "
            "and leaves your trades to it.[/]"
        )
    monitor = Monitor(
        config, broker, symbols, live_flag=args.live, journal_path=config.journal, notifier=notifier,
        book=book, duty=duty,
    )  # fmt: skip
    active = book.active()
    if active:
        console.print(
            f"Sizes and loss limits are worked out for your account [bold]{escape(active)}[/]; "
            f"orders are placed by you at your broker."
        )
    names = {broker_name: wanted for wanted in config.symbols if (broker_name := broker.resolve(wanted))}
    worker = threading.Thread(target=monitor.run, name="mt5", daemon=True)
    try:
        server = open_dashboard(monitor, args.port)
    except ValueError:
        broker.shutdown()
        raise
    worker.start()
    monitor.heartbeat = True
    notifier.listen(monitor.command)  # /calc, /news, /status ... from your own Telegram chat
    monitor.news_feed = _news_feed(config)
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
        if duty.primary:
            notifier.send(
                "STOPPED · tradebot\nThe dashboard was closed: no signal alerts until it is started again. "
                "If the trading loop (tradebot run) is running, it takes over the reversal warnings and "
                "your trades' stops within one of its cycles.",
                "status",
            )
        monitor.tracks.close()  # a backtest still measuring for the record is not left behind
        worker.join(timeout=10)
        duty.close()  # after the worker: no stop is moved once the loop may have taken over
        notifier.close()
        book.close()
        if monitor.news_feed:
            monitor.news_feed.stop()
        broker.shutdown()
    return 0


def cmd_account(config: BotConfig, args: argparse.Namespace) -> int:
    """Your own accounts, kept in the journal file. No terminal is needed for this."""
    from tradebot.book import Book

    book = Book(config.journal)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        if args.mt5:
            book.use(None)
        elif args.name and args.remove:
            book.remove_account(args.name)
        elif args.name and args.equity is not None:
            # The balance is what moves when a logged trade closes. With trades open, set the
            # equity from the dashboard or Telegram instead: they take the floating result out.
            saved = book.save_account(args.name, args.equity, now, args.min_lot, args.lot_step, args.leverage)
            if args.use:  # saving never switches the account in use by itself
                book.use(saved.name)
        elif args.name and args.use:
            book.use(args.name)
        elif args.name:
            raise ValueError("give the equity too, for example: tradebot account Exness 500")
        accounts, active = book.accounts(), book.active()
        if not accounts:
            console.print(
                "No accounts yet. Add one with its equity: [bold]tradebot account Exness 500[/]\n"
                "The dashboard then sizes every trade from that figure instead of the terminal's account."
            )
            return 0
        table = Table(box=None, header_style="bold grey50", pad_edge=False)
        for col in ("", "Account", "Balance", "Open trades", "Min lot", "Lot step", "Leverage", "Set"):
            table.add_column(col, justify="right" if col in ("Balance", "Open trades") else "left")
        for a in accounts:
            table.add_row(
                "[green]in use[/]" if a.name == active else "", escape(a.name), f"{a.balance:,.2f}",
                str(len(book.trades(a.name, open_only=True))),
                "terminal's" if a.min_lot is None else f"{a.min_lot:g}",
                "terminal's" if a.lot_step is None else f"{a.lot_step:g}",
                "not set" if a.leverage is None else f"1:{a.leverage:g}", f"{a.updated:%Y-%m-%d %H:%M} UTC",
            )  # fmt: skip
        console.print(table)
        if active is None:
            console.print(
                "[grey50]The MetaTrader 5 account is in use. `tradebot account NAME --use` switches.[/]"
            )
    finally:
        book.close()
    return 0


def cmd_fetch(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.broker.mt5 import fetch_history
    from tradebot.data import bars_path, save_spec, trend_timeframes

    broker, _, _ = _connect(config)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        for configured in config.symbols:
            # Resolved one by one: pairing the names by position put the next symbol's history in
            # this one's file whenever a configured symbol was missing at the broker.
            symbol = broker.resolve(configured)
            if symbol is None:
                continue  # _connect has already warned about it
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
        f"Backtest · {setup_text(config)} · {config.timeframe}",
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
    name = "+".join(n for n, _ in config.setup_groups())  # e.g. ema_cross+breakout
    report = write_report(render_backtest(result), args.out, f"backtest_{name}_{config.timeframe}.html")
    trades_path = args.out / f"trades_{name}_{config.timeframe}.csv"
    _write_trades(result.trades, trades_path)
    console.print(f"\n[grey50]Report[/]  {report}\n[grey50]Trades[/]  {trades_path}")
    return 0


def cmd_optimize(config: BotConfig, args: argparse.Namespace) -> int:
    from tradebot.backtest import run_backtest
    from tradebot.optimize import apply_params, walk_forward
    from tradebot.report import render_backtest, write_report

    data, specs, htf = _load(config, args)
    with console.status(f"Walk-forward: {setup_text(config)}, {args.folds} folds…"):
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
        f"Out-of-sample (stitched) · {setup_text(config)}",
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
        # written as a dry run whatever the source config says: "review before trading it" must
        # not be a config that trades as soon as it is used
        tuned = replace(apply_params(config, wf.final_params), dry_run=True, live_trading_confirm=False)
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


class _SeenStops:
    """The terminal, with the first stop the dashboard saw on a trade that opened without one
    (a phone trade often gets its stop a moment later). The dashboard's review already used it;
    without it ``tradebot review`` could not measure those trades in R."""

    def __init__(self, inner: Any, book: Any) -> None:
        self.inner, self.book = inner, book

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def opening(self, ticket: int) -> tuple[float | None, float | None]:
        volume, stop = self.inner.opening(ticket)
        if stop or not self.book:
            return volume, stop
        # First stops are kept per account (login, server), as the dashboard writes them: looking
        # one up by ticket alone found only the old unscoped ones (audit of 23 Nov 2026).
        account = getattr(self.inner, "account_id", lambda: None)()
        return volume, self.book.first_stop(ticket, account)


def cmd_review(config: BotConfig, args: argparse.Namespace) -> int:
    from datetime import timedelta

    from tradebot.review import closed_trades, summarize, verdict

    broker, _, _ = _connect(config)
    book = None
    try:
        now = broker.now()
        since = now - timedelta(days=args.days)
        from tradebot.book import Book, BookBroker

        if args.account:  # the trades you logged for one of your own accounts
            book = Book(config.journal)
            account = book.account(args.account)
            if account is None:
                raise ValueError(f"no account named {args.account!r}: see `tradebot account`")
            own = BookBroker(broker, book, config.magic)
            with own.using(account.name):
                trades = closed_trades(own, config.magic, since)
        else:
            scope = None if config.risk.account_trades else config.magic  # phone trades count too
            book = Book(config.journal) if Path(config.journal).is_file() else None
            trades = closed_trades(_SeenStops(broker, book), scope, since, bot_magic=config.magic)
    finally:
        if book:
            book.close()
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
