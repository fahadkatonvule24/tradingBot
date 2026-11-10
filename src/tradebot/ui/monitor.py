"""The dashboard's worker: owns the MT5 connection and keeps a JSON-ready snapshot.

Every MT5 call happens on this one thread (the MetaTrader5 package is not built for concurrent
use). Each cycle reads ticks, the account and positions; signals are recomputed only when a new
bar has closed. Orders placed from the page are queued here and executed between cycles, after
re-checking the advice and the trading gate at that moment, never trusting what the browser sent.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradebot import __version__
from tradebot.advisor import WORD, Advice, advise, pip_size, recent_signals
from tradebot.broker.base import Broker, OrderRequest, SymbolSpec
from tradebot.config import BotConfig
from tradebot.engine import Engine
from tradebot.journal import Journal, JournalEvent
from tradebot.live import Link, latest_features, preview_features, trading_gate

log = logging.getLogger(__name__)

SPARK_BARS = 48
CHART_BARS = 120
SPEC_REFRESH = timedelta(minutes=5)
PREVIEW_SECONDS = 3.0
LOST_LINK = "Lost the connection to MetaTrader 5. Is the terminal open? Prices are frozen; retrying."


@dataclass
class _Order:
    symbol: str
    side: int
    manual: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] = field(default_factory=dict)


class Monitor:
    def __init__(
        self,
        config: BotConfig,
        broker: Broker,
        symbols: list[str],
        *,
        live_flag: bool = False,
        journal_path: str | Path | None = None,
        interval: float = 1.0,
        notifier: Any = None,
    ) -> None:
        self.config, self.broker, self.symbols = config, broker, symbols
        self.live_flag = live_flag
        self.interval = interval
        self.engine = Engine(config, broker, execute=False, live=True)  # guards and sizing only
        self.track_record: dict[str, Any] = {}
        self._journal_path = journal_path
        self._journal: Journal | None = None
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"ready": False, "version": __version__}
        self._features: dict[str, pd.DataFrame] = {}
        self._fetched: dict[str, float] = {}
        self._recent: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._link = Link(broker)
        self._last_error: str | None = None
        self.notifier = notifier
        self._previews: dict[str, tuple[float, pd.DataFrame | None]] = {}
        self._forming_seen: set[tuple[str, Any, int]] = set()
        self._specs: dict[str, tuple[SymbolSpec, datetime]] = {}
        self._last_action: dict[str, str] = {}
        self._orders: queue.Queue[_Order] = queue.Queue()
        self._stop = threading.Event()
        self.activity: deque[dict[str, Any]] = deque(maxlen=50)

    # --- read side (web server threads) -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._state

    def chart(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            features = self._features.get(symbol)
        if features is None:
            return None
        p = self.config.for_symbol(symbol).strategy
        tail = features.tail(CHART_BARS)
        bars = tail[["time", "open", "high", "low", "close", "ema_fast", "ema_slow"]].to_dict("records")
        return clean({"symbol": symbol, "fast": p.fast_ema, "slow": p.slow_ema, "bars": bars})

    def submit(self, symbol: str, side: int, manual: bool = False, timeout: float = 20.0) -> dict[str, Any]:
        """Queue an order for the worker thread and wait for its outcome."""
        order = _Order(symbol, side, manual)
        self._orders.put(order)
        if not order.done.wait(timeout):
            return {"ok": False, "message": "timed out waiting for the MT5 thread"}
        return order.result

    # --- worker thread -------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if self._journal_path:
            self._journal = Journal(self._journal_path)  # sqlite objects stay on the thread that made them
        try:
            while not self._stop.is_set():
                deadline = time.monotonic() + self.interval
                self.cycle()
                while (remaining := deadline - time.monotonic()) > 0 and not self._stop.is_set():
                    try:
                        order = self._orders.get(timeout=remaining)
                    except queue.Empty:
                        break
                    order.result = self._safe_execute(order)
                    order.done.set()
        finally:
            if self._journal:
                self._journal.close()

    def cycle(self) -> None:
        if not self._link.ok():
            return self._report(LOST_LINK)
        try:
            self.refresh()
        except Exception as exc:  # keep the page alive and say what went wrong
            if not self._link.ok():
                return self._report(LOST_LINK)
            message = f"{type(exc).__name__}: {exc}"
            if message != self._last_error:  # the same failure every second is logged once
                log.exception("dashboard refresh failed")
            self._last_error = message
            self._report(message)
        else:
            self._last_error = None

    def _report(self, error: str) -> None:
        with self._lock:
            self._state = {**self._state, "error": error}

    def refresh(self) -> None:
        now = self.broker.now()
        account = self.broker.account()
        terminal_allows = getattr(self.broker, "terminal_trading_allowed", lambda: False)()
        gate = trading_gate(self.config, account, self.live_flag, terminal_allows)
        rows = []
        for symbol in self.symbols:
            try:
                rows.append(self._symbol_state(symbol, now))
            except Exception as exc:
                log.warning("%s: %s", symbol, exc)
                rows.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
        positions = []
        for p in self.broker.positions(None):
            r = None
            if p.magic == self.config.magic and p.initial_risk:
                price = next((row["bid"] if p.side > 0 else row["ask"]
                              for row in rows if row.get("symbol") == p.symbol and "bid" in row), None)  # fmt: skip
                r = (price - p.price_open) * p.side / p.initial_risk if price is not None else None
            positions.append(asdict(p) | {"bot": p.magic == self.config.magic, "r": r})
        server = getattr(self.broker, "server_name", lambda: "")()
        state = {
            "ready": True,
            "version": __version__,
            "now": now,
            "strategy": self.config.strategy.name,
            "timeframe": self.config.timeframe,
            "session": "{:02d}-{:02d} UTC".format(*self.config.schedule.hours()),
            "account": asdict(account) | {"server": server, "floating": account.equity - account.balance},
            "gate": {"execute": gate.execute, "reason": gate.reason, "live_flag": self.live_flag},
            "symbols": rows,
            "positions": positions,
            "activity": list(self.activity)[::-1],
            "track_record": self.track_record,
            "error": None,
        }
        with self._lock:
            self._state = clean(state)

    def _symbol_state(self, symbol: str, now: datetime) -> dict[str, Any]:
        spec = self._spec(symbol, now)
        tick = self.broker.tick(symbol)
        features = self._features_for(symbol, now)
        preview = self._preview_for(symbol, features)
        advice = advise(
            self.engine, symbol, spec, self.config.for_symbol(symbol), features, tick, now, preview
        )
        self._note(symbol, advice, now, spec)
        self._note_forming(symbol, advice, now, spec)
        last = features.iloc[-1]
        forming = getattr(self.broker, "forming_bar", None)
        return {
            "symbol": symbol,
            "digits": spec.digits,
            "pip": pip_size(spec),
            "pip_label": "pips" if spec.digits in (3, 5) else "points",
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": tick.ask - tick.bid,
            "tick_time": tick.time,
            "prev_close": float(last["close"]),
            "atr": float(last["atr"]),
            "spark": [*features["close"].tail(SPARK_BARS).round(spec.digits).tolist(), tick.bid],
            "forming": forming(symbol, self.config.timeframe) if forming else None,
            "advice": advice,
            "history": self._history(symbol, features),
        }

    def _history(self, symbol: str, features: pd.DataFrame) -> dict[str, Any]:
        """Recent signals and their simple outcomes, recomputed when a new bar arrives."""
        key = features["time"].iloc[-1]
        cached = self._recent.get(symbol)
        if cached is None or cached[0] != key:
            params = self.config.for_symbol(symbol)
            cached = (key, recent_signals(features, params, self.config.schedule, self.config.bar_minutes))
            self._recent[symbol] = cached
        return cached[1]

    def _spec(self, symbol: str, now: datetime) -> SymbolSpec:
        cached = self._specs.get(symbol)
        if cached is None or now - cached[1] > SPEC_REFRESH:
            cached = (self.broker.symbol(symbol), now)
            self._specs[symbol] = cached
        return cached[0]

    def _features_for(self, symbol: str, now: datetime) -> pd.DataFrame:
        """Recompute signals when a new bar should have closed (rate-limited while the market is shut)."""
        features = self._features.get(symbol)
        fetched = self._fetched.get(symbol, 0.0)
        if features is not None:
            bar = pd.Timedelta(minutes=self.config.bar_minutes)
            next_close = features["time"].iloc[-1] + 2 * bar
            due = pd.Timestamp(now) >= next_close and time.monotonic() - fetched >= 5
            if not due and time.monotonic() - fetched < 300:
                return features
        features = latest_features(self.broker, self.config, symbol)
        self._fetched[symbol] = time.monotonic()
        with self._lock:
            self._features[symbol] = features
        return features

    def _preview_for(self, symbol: str, features: pd.DataFrame) -> pd.DataFrame | None:
        """The forming bar's would-be signals, refreshed every few seconds (early warning only)."""
        cached = self._previews.get(symbol)
        if cached and time.monotonic() - cached[0] < PREVIEW_SECONDS:
            return cached[1]
        try:
            preview = preview_features(self.broker, self.config, symbol, features)
        except Exception:
            log.debug("%s: preview failed", symbol, exc_info=True)
            preview = None
        self._previews[symbol] = (time.monotonic(), preview)
        return preview

    def _note_forming(self, symbol: str, advice: Advice, now: datetime, spec: SymbolSpec) -> None:
        """Announce a forming trigger once per bar and side. It flickers; that is its nature."""
        early = advice.early
        if not early or not early["firing"] or advice.actionable:
            return
        key = (symbol, early["closes_at"], early["side"])
        if key in self._forming_seen:
            return
        if len(self._forming_seen) > 500:
            self._forming_seen.clear()
        self._forming_seen.add(key)
        word = WORD[early["side"]]
        where = ""
        if early["level"] is not None:
            where = f" if the {self.config.timeframe} bar closes {'above' if early['side'] > 0 else 'below'} {early['level']:.{spec.digits}f}"
        verdict = (
            "the filters would pass it"
            if early["passes"]
            else f"the filters would block it ({early['blocked_by']})"
        )
        text = (
            f"{word} trigger forming{where}. Decided at {early['closes_at']:%H:%M} UTC; "
            f"price now {early['price']:.{spec.digits}f}; {verdict}. Not confirmed, it can still disappear."
        )
        self.activity.append({"time": now, "symbol": symbol, "action": "FORMING", "text": text})
        self._send(f"FORMING (unconfirmed) · {symbol}\n{text}")

    def _send(self, text: str) -> None:
        if self.notifier:
            self.notifier.send(text)

    def _note(self, symbol: str, advice: Advice, now: datetime, spec: SymbolSpec | None = None) -> None:
        """Log advice changes worth seeing: new signals, skips and the return to waiting."""
        key = f"{advice.action}:{advice.bar_time}"
        previous = self._last_action.get(symbol)
        self._last_action[symbol] = key
        if previous == key or (previous is None and advice.action in ("WAIT", "CLOSED")):
            return
        if previous is not None and previous.split(":")[0] == advice.action and advice.action == "WAIT":
            return
        self.activity.append(
            {
                "time": now,
                "symbol": symbol,
                "action": advice.action,
                "text": f"{advice.headline}. {advice.detail}",
            }
        )
        if advice.action in ("BUY", "SELL", "BLOCKED", "SKIP"):
            self._send(self._alert_text(symbol, advice, spec))

    @staticmethod
    def _alert_text(symbol: str, advice: Advice, spec: SymbolSpec | None) -> str:
        digits = spec.digits if spec else 5
        lines = [f"{advice.action} · {symbol}", f"{advice.headline}. {advice.detail}"]
        plan = advice.plan
        if plan and plan.volume and advice.action != "SKIP":
            lines.append(
                f"{WORD[plan.side]} plan: entry ~{plan.entry:.{digits}f} · SL {plan.stop:.{digits}f} · "
                f"TP {plan.target:.{digits}f} · {plan.volume} lots · risk {plan.risk_amount:,.2f} · "
                f"target {plan.reward_amount:,.2f}"
            )
        return "\n".join(lines)

    # --- orders --------------------------------------------------------------------------------

    def _safe_execute(self, order: _Order) -> dict[str, Any]:
        try:
            return self.execute(order.symbol, order.side, order.manual)
        except Exception as exc:
            log.exception("order failed")
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    def execute(self, symbol: str, side: int, manual: bool = False) -> dict[str, Any]:
        """Place a trade if, right now, the gate is open and the engine's risk rules allow it.

        A bot trade must match the current advice. A manual trade is the user's own call on
        direction: no strategy signal is required, but the trading gate, every entry guard
        (session, news, loss limits, positions, spread) and the engine's sizing still apply.
        """
        if symbol not in self.symbols or side not in WORD:
            return {"ok": False, "message": "unknown symbol or side"}
        now = self.broker.now()
        account = self.broker.account()
        gate = trading_gate(self.config, account, self.live_flag, self.broker.terminal_trading_allowed())
        if not gate.execute:
            return {"ok": False, "message": f"orders are disabled: {gate.reason}"}
        spec = self.broker.symbol(symbol)
        tick = self.broker.tick(symbol)
        features = self._features_for(symbol, now)
        advice = advise(self.engine, symbol, spec, self.config.for_symbol(symbol), features, tick, now)
        if manual:
            plan = advice.plans.get("buy" if side > 0 else "sell")
            if plan is None:
                return {"ok": False, "message": f"no plan available: {advice.headline}"}
            if advice.blocker or plan.rejected:
                return {"ok": False, "message": f"blocked by a risk rule: {advice.blocker or plan.rejected}"}
        else:
            plan = advice.plan
            if not advice.actionable or advice.side != side or plan is None or plan.rejected:
                return {"ok": False, "message": f"not advised any more: {advice.headline}. {advice.detail}"}

        word = WORD[side]
        comment = f"{self.config.comment} manual" if manual else self.config.comment
        request = OrderRequest(symbol, side, plan.volume, plan.stop_distance, plan.target_distance, comment)
        result = self.broker.open(request, self.config.magic)
        reason = "manual: the user's call from the dashboard" if manual else "advisor: " + advice.detail
        if self._journal:
            self._journal.record(JournalEvent(
                now, "OPEN" if result.ok else "REJECTED", symbol, word, plan.volume, result.price,
                plan.stop, plan.target, result.ticket, None, reason if result.ok else result.message, False,
            ))  # fmt: skip
        text = (
            f"{'Manual ' if manual else ''}{word} {plan.volume} @ {result.price} · SL {plan.stop} · TP {plan.target}"
            if result.ok
            else f"{word} rejected by the broker: {result.message}"
        )
        self.activity.append(
            {"time": now, "symbol": symbol, "action": "ORDER" if result.ok else "REJECTED", "text": text}
        )
        log.warning("%s %s", symbol, text)
        self._send(f"ORDER · {symbol}\n{text}")
        return {"ok": result.ok, "message": text, "ticket": result.ticket, "price": result.price}

    # --- history -------------------------------------------------------------------------------

    def load_track_record(self, data_dir: str | Path, names: dict[str, str]) -> None:
        """Backtest the config on stored history so each symbol's advice shows its record.

        ``names`` maps the broker's symbol names to the configured ones used for the CSV files.
        """
        from tradebot.backtest import run_backtest
        from tradebot.data import load_dataset, load_trend_bars

        try:
            data, specs = load_dataset(data_dir, list(self.config.symbols), self.config.timeframe)
            htf = load_trend_bars(data_dir, self.config)
            result = run_backtest(self.config, data, specs, htf=htf)
        except (FileNotFoundError, ValueError) as exc:
            log.info("no track record: %s", exc)
            return
        per = result.per_symbol
        m = result.metrics
        record = {
            "start": result.start,
            "end": result.end,
            "trades": m.trades,
            "return_pct": m.return_pct,
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor,
            "avg_r": m.avg_r,
            "symbols": {
                broker_name: {
                    "trades": int(per.loc[configured, "trades"]),
                    "win_rate": float(per.loc[configured, "win_rate"]),
                    "profit_factor": float(per.loc[configured, "profit_factor"]),
                    "avg_r": float(per.loc[configured, "avg_r"]),
                }
                for broker_name, configured in names.items()
                if configured in per.index
            },
        }
        self.track_record = clean(record)


def clean(value: Any) -> Any:
    """JSON-safe copy: datetimes as UTC ISO strings, numpy scalars as Python, NaN/inf as None."""
    if is_dataclass(value) and not isinstance(value, type):
        return clean(asdict(value))
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
