"""Run the real dashboard against disposable, synthetic data, without MetaTrader 5.

    python scripts/preview_dashboard.py
    python scripts/preview_dashboard.py --port 8766 --at 2026-11-17T12:07:00

Prices are seeded examples, not market quotes. The clock advances from --at (UTC), or
the current UTC time. Timeframes, strategies, calculator and an in-memory account book
use the normal application code. Nothing reads the user's config, journal, saved data,
credentials or news feed. All preview changes disappear when the process stops.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from tradebot.book import Book
from tradebot.broker.base import Deal, OrderRequest, OrderResult, Position
from tradebot.broker.sim import SimBroker
from tradebot.config import TIMEFRAME_MINUTES, BacktestParams, BotConfig, ScheduleParams, StrategyParams
from tradebot.data import preset_spec, synthetic_bars
from tradebot.news import NewsCalendar, NewsEvent
from tradebot.ui.monitor import Monitor
from tradebot.ui.server import DashboardServer

SERIES = {
    "EURUSD": (1.08472, 11),
    "GBPUSD": (1.27384, 23),
    "USDJPY": (150.382, 37),
    "AUDUSD": (0.65428, 41),
    "USDCAD": (1.38126, 53),
    "XAUUSD": (2648.35, 67),
}
REFUSAL = "Synthetic preview: orders are disabled. No broker is connected."


class PreviewBroker(SimBroker):
    """Reuse simulated valuation and sizing, with seeded history and no execution."""

    def __init__(self, start: datetime) -> None:
        specs = {name: preset_spec(name) for name in SERIES}
        params = {name: BacktestParams(min_spread_points=12) for name in SERIES}
        super().__init__(specs, params, initial_balance=24_850.0)
        self.start = start
        self.started = time.monotonic()
        self.frames: dict[tuple[str, str], tuple[pd.Timestamp, pd.DataFrame]] = {}
        for name in SERIES:
            self._bars[name] = self.bars(name, "M15", 1).iloc[-1]
        for ticket, (name, side, move, volume) in enumerate(
            (("EURUSD", 1, 0.00115, 0.20), ("XAUUSD", -1, -7.4, 0.10)), 1
        ):
            price = SERIES[name][0]
            distance = 0.003 if name == "EURUSD" else 24.0
            entry = self.specs[name].round_price(price - move)
            stop = self.specs[name].round_price(entry - side * distance)
            self._positions[ticket] = Position(
                ticket=ticket,
                symbol=name,
                side=side,
                volume=volume,
                price_open=entry,
                sl=stop,
                tp=self.specs[name].round_price(entry + side * distance * 2),
                time_open=start - timedelta(minutes=45),
                initial_volume=volume,
                initial_sl=stop,
                comment="Synthetic preview position",
            )
        self._deals = [
            Deal(100, 100, "GBPUSD", start - timedelta(minutes=90), 1, 0.1, 1.27, 112.5, "out"),
            Deal(101, 101, "AUDUSD", start - timedelta(minutes=70), -1, 0.1, 0.65, -32.0, "out"),
        ]
        self._deal_times = [deal.time for deal in self._deals]

    def now(self) -> datetime:
        return self.start + timedelta(seconds=time.monotonic() - self.started)

    def bars(self, name: str, timeframe: str, count: int) -> pd.DataFrame:
        minutes = TIMEFRAME_MINUTES[timeframe]
        boundary = pd.Timestamp(self.now()).floor(f"{minutes}min")
        key = (name, timeframe)
        cached = self.frames.get(key)
        if cached is None or cached[0] != boundary:
            price, seed = SERIES[name]
            frame = synthetic_bars(
                n=1500,
                seed=seed + minutes,
                price=price,
                volatility=0.00045 * (minutes / 15) ** 0.35,
                trend_strength=0.00005,
                spread_points=12,
            )
            factor = price / float(frame["close"].iloc[-1])
            columns = ["open", "high", "low", "close"]
            frame[columns] = (frame[columns] * factor).round(self.specs[name].digits)
            frame["time"] = pd.date_range(
                end=boundary - pd.Timedelta(minutes=minutes), periods=len(frame), freq=f"{minutes}min"
            )
            frame["tick_volume"] = np.random.default_rng(seed).integers(180, 1200, len(frame))
            cached = (boundary, frame)
            self.frames[key] = cached
        return cached[1].tail(count).copy()

    def forming_bar(self, name: str, timeframe: str) -> dict[str, Any]:
        frame = self.bars(name, timeframe, 1)
        bar = frame.iloc[-1].to_dict()
        price = SERIES[name][0]
        bar.update(
            time=bar["time"] + pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe]),
            open=price,
            high=price,
            low=price,
            close=price,
            tick_volume=125,
        )
        return bar

    def positions(self, magic: int | None) -> list[Position]:
        positions = [p for p in self._positions.values() if magic is None or p.magic == magic]
        for position in positions:
            position.profit = self._mark(position)
        return positions

    def server_name(self) -> str:
        return "SYNTHETIC PREVIEW"

    def terminal_trading_allowed(self) -> bool:
        return False

    def open(self, request: OrderRequest, magic: int) -> OrderResult:
        return OrderResult(False, message=REFUSAL)

    def close(self, position: Position, volume: float | None, magic: int, comment: str) -> OrderResult:
        return OrderResult(False, message=REFUSAL)

    def modify(self, position: Position, sl: float, tp: float, magic: int) -> OrderResult:
        return OrderResult(False, message=REFUSAL)


class PreviewCalendar(NewsCalendar):
    def __init__(self, start: datetime) -> None:
        super().__init__(None)
        self.samples = [
            NewsEvent(start + timedelta(minutes=48), "USD", "high", "Retail sales (sample)", "0.3%", "0.2%"),
            NewsEvent(start + timedelta(hours=2), "GBP", "medium", "Inflation outlook (sample)"),
            NewsEvent(start + timedelta(hours=3), "CAD", "high", "Consumer prices (sample)", "2.1%", "2.0%"),
        ]

    def events(self) -> list[NewsEvent]:
        return self.samples


class PreviewMonitor(Monitor):
    def __init__(self, *args: Any, calendar: PreviewCalendar, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calendar = calendar

    def refresh(self) -> None:
        # Reapply after switching timeframe/strategy, which replaces the decision engine.
        self.engine._news = self.calendar
        super().refresh()
        with self._lock:
            self._state["preview"] = True
            self._state["gate"] = {"execute": False, "reason": REFUSAL, "live_flag": False}

    def submit(self, symbol: str, side: int, manual: bool = False, timeout: float = 20.0) -> dict[str, Any]:
        return {"ok": False, "message": REFUSAL}


class PreviewServer(DashboardServer):
    def page(self) -> bytes:
        return super().page().replace(b"<title>", b"<title>PREVIEW | ", 1)


def utc_time(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--at", type=utc_time, help="Starting UTC time (ISO 8601); default: now")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    start = args.at or datetime.now(timezone.utc).replace(tzinfo=None)
    config = BotConfig(
        symbols=tuple(SERIES),
        history_bars=600,
        dry_run=True,
        kill_switch_file="",
        journal=":memory:",
        strategy=StrategyParams(trend_timeframes="auto"),
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file="", news_feed=""),
    )
    book = Book(":memory:")
    book.save_account("Preview account", 10_000.0, start, leverage=100)
    monitor = PreviewMonitor(
        config,
        PreviewBroker(start),
        list(SERIES),
        calendar=PreviewCalendar(start),
        book=book,
        interval=2.0,
    )
    monitor.activity.append(
        {
            "time": start,
            "symbol": "PREVIEW",
            "action": "INFO",
            "text": "Synthetic UI preview. No market connection; all account edits stay in memory.",
        }
    )
    monitor.refresh()
    server = PreviewServer(monitor, port=args.port)
    worker = threading.Thread(target=monitor.run, name="preview-monitor", daemon=True)
    worker.start()
    print(f"Synthetic dashboard preview: {server.url}", flush=True)
    print("No MT5 connection, no orders, no saved user data. Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        monitor.stop()
        worker.join(timeout=10)
        if not worker.is_alive():
            book.close()


if __name__ == "__main__":
    main()
