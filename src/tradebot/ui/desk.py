"""The dashboard's side of the book and of the market view. Everything here runs on the MT5 thread.

``Desk`` is what a person does with their own accounts: set an equity, log a trade taken on the
phone, close it, correct it. The same operations are reachable from the page (``op``) and from
Telegram (``command``), because the phone is where the trades are placed.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from tradebot import calculator, market
from tradebot.advisor import WORD, trade_plan
from tradebot.book import OWN_CALL, SIGNAL, Book, BookBroker, BookError, BookTrade
from tradebot.engine import Engine
from tradebot.review import closed_trades, summarize, verdict

if TYPE_CHECKING:
    from tradebot.ui.monitor import Monitor

log = logging.getLogger(__name__)

REVIEW_DAYS = 90
MT5_REVIEW_SECONDS = 60.0
NEWS_HOURS = 36
LEVELS_SECONDS = 5.0
DAILY_SECONDS = 900.0
HELP = (
    "Commands\n"
    "/calc SYMBOL buy|sell [risk=1] [lots=0.1] [sl=PRICE] [pips=20] [tp=PRICE] [lev=500] [equity=1000] - size, margin and leverage\n"
    "/news - high- and medium-impact news for your instruments in the next 36 hours\n"
    "/equity NAME AMOUNT [lev=500] - add one of your accounts or set its equity and leverage\n"
    "/use NAME - work everything out for this account (/use mt5 for the terminal's)\n"
    "/accounts - your accounts and their equity\n"
    "/plan SYMBOL buy|sell - entry, stop, target and the size at each account\n"
    "/took SYMBOL buy|sell [lots=0.02] [entry=1.0850] [sl=1.0820] [tp=1.0910] [account=NAME] - log a trade you placed\n"
    "/close ID [PRICE] [pnl=AMOUNT] - log that a trade is closed\n"
    "/sl ID PRICE, /tp ID PRICE - you moved the stop or target\n"
    "/open - open trades in the book\n"
    "/status - loss limits and open risk\n"
    "/market - currency strength, your exposure, sessions\n"
    "/analysis SYMBOL - market assessment, conflicts and both scenarios\n"
    "/levels SYMBOL - yesterday's and this week's levels"
)


def _number(body: dict[str, Any], key: str, required: bool = False) -> float | None:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise BookError(f"{key} is required")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BookError(f"{key} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise BookError(f"{key} must be a number")
    return number


def _side(value: Any) -> int:
    text = str(value).strip().lower()
    if text in ("1", "buy", "long", "b"):
        return 1
    if text in ("-1", "sell", "short", "s"):
        return -1
    raise BookError("say buy or sell")


class Desk:
    def __init__(self, monitor: Monitor, book: Book) -> None:
        self.m, self.book = monitor, book
        self.broker: BookBroker = monitor.broker  # type: ignore[assignment]
        self._sizes: dict[str, tuple[Any, dict[str, list[dict[str, Any]]]]] = {}
        self._swept: dict[int, Any] = {}
        self._history: tuple[Any, dict[str, Any]] | None = None
        self._mt5_read = 0.0

    @property
    def manual(self) -> bool:
        return self.broker.account_name() is not None

    def _money(self, amount: float, signed: bool = False) -> str:
        return f"{amount:{'+' if signed else ''},.2f} {self.m.currency}"

    def _digits(self, symbol: str) -> int:
        return self.broker.symbol(symbol).digits

    # --- operations -------------------------------------------------------------------------------

    def op(self, body: dict[str, Any]) -> dict[str, Any]:
        """One change to the book, requested by the page."""
        handlers = {
            "account": lambda b: self.set_equity(
                str(b.get("name", "")), _number(b, "equity", True), _number(b, "min_lot"), _number(b, "lot_step"),
                leverage=_number(b, "leverage"),
            ),
            "use": lambda b: self.use(b.get("name") or None),
            "remove": lambda b: self.remove(str(b.get("name", ""))),
            "log": lambda b: self.log(
                str(b.get("symbol", "")), _side(b.get("side")), _number(b, "volume"), _number(b, "entry"),
                _number(b, "sl"), _number(b, "tp"), b.get("account") or None, str(b.get("note") or ""),
            ),
            "close": lambda b: self.close(int(b["id"]), _number(b, "price"), _number(b, "pnl")),
            "amend": lambda b: self.amend(int(b["id"]), _number(b, "sl"), _number(b, "tp")),
            "result": lambda b: self.result(int(b["id"]), _number(b, "pnl", True)),
            "reopen": lambda b: self.reopen(int(b["id"])),
            "delete": lambda b: self.delete(int(b["id"])),
        }  # fmt: skip
        handler = handlers.get(str(body.get("op")))
        if handler is None:
            return {"ok": False, "message": f"unknown operation {body.get('op')!r}"}
        try:
            return {"ok": True, "message": handler(body)}
        except (BookError, KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            self.m.book_changed()

    def set_equity(
        self,
        name: str,
        equity: float | None,
        min_lot: float | None = None,
        lot_step: float | None = None,
        keep_rules: bool = False,
        leverage: float | None = None,
    ) -> str:
        """``equity`` is what the broker's app shows now, open trades included.

        Saving an account never switches to it: the account in use changes only on request.
        """
        if equity is None:
            raise BookError("give the equity")
        existing = self.book.account(name)
        floating = self.broker.floating_total(existing.name) if existing else 0.0
        if existing and keep_rules:
            min_lot, lot_step = existing.min_lot, existing.lot_step
            leverage = existing.leverage if leverage is None else leverage
        account = self.book.save_account(
            name, equity - floating, self.broker.now(), min_lot, lot_step, leverage
        )
        text = f"{account.name}: equity set to {self._money(equity)}" + (
            f", leverage 1:{account.leverage:g}." if account.leverage else "."
        )
        if self.broker.account_name() != account.name:
            using = self.broker.account_name() or "the MetaTrader 5 account"
            text += f" Still using {using}; choose Use (or /use {account.name}) to size for this one."
        return text

    def use(self, name: str | None) -> str:
        if name and str(name).lower() in ("mt5", "terminal", "none"):
            name = None
        chosen = self.book.use(name)
        if chosen is None:
            return "Now using the MetaTrader 5 account in the terminal."
        return f"Now using {chosen}: sizes, loss limits and open trades are this account's."

    def remove(self, name: str) -> str:
        self.book.remove_account(name)
        return f"{name} removed. Its closed trades stay in the book."

    def log(
        self,
        symbol: str,
        side: int,
        volume: float | None = None,
        entry: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        account: str | None = None,
        note: str = "",
    ) -> str:
        """Record a trade placed at a broker. Whatever is left out is taken from the plan."""
        symbol = self.m.match_symbol(symbol)
        owned = self.book.accounts()
        name = account or self.broker.account_name() or (owned[0].name if len(owned) == 1 else None)
        if not name:
            raise BookError(
                "say which account it was placed at (account=NAME), or add one first: the size is worked "
                "out from its equity"
            )
        owner = self.book.account(name)
        if owner is None:
            raise BookError(f"no account named {name!r}")
        with self.broker.using(owner.name):
            advice = self.m.advice_now(symbol)
            spec = self.broker.symbol(symbol)
        plan = advice.plans.get("buy" if side > 0 else "sell")
        if plan is None and (entry is None or volume is None):
            raise BookError("the market is closed, so there is no plan to copy: give lots and entry")
        if volume is None:
            if not plan.volume:
                raise BookError(f"the plan has no size ({plan.rejected}): give the lots you traded")
            volume = plan.volume
        if entry is None:
            entry = plan.entry
        if plan is not None:  # the plan's distances, from the price you actually got
            sl = spec.round_price(entry - side * plan.stop_distance) if sl is None else sl
            tp = spec.round_price(entry + side * plan.target_distance) if tp is None else tp
        source = SIGNAL if advice.actionable and advice.side == side else OWN_CALL
        with self.broker.using(owner.name):
            trade = self.book.open_trade(
                owner.name, symbol, side, volume, entry, self.broker.now(), sl, tp, source, note
            )
            risk = self._risk(trade)
        d = spec.digits
        text = (
            f"Logged #{trade.id}: {WORD[side]} {trade.volume:g} {symbol} at {entry:.{d}f} on {owner.name}"
            + (f", stop {trade.sl:.{d}f}" if trade.sl else ", no stop")
            + (f", target {trade.tp:.{d}f}" if trade.tp else "")
            + "."
        )
        if risk:
            text += f" At the stop it loses {self._money(risk)}."
        self.m.note_book(symbol, "LOGGED", text)
        return text

    def close(self, trade_id: int, price: float | None = None, pnl: float | None = None) -> str:
        trade = self.book.trade(trade_id)
        if price is None:
            price = self.broker.exit_price(trade)
        if pnl is None:
            pnl = self.broker.value(trade.symbol, trade.side, trade.volume, trade.entry, price)
        if trade.sl and (price - trade.sl) * trade.side <= 0:
            reason = "stop" if trade.sl == trade.initial_sl else "trailing stop"
        elif trade.tp and (price - trade.tp) * trade.side >= 0:
            reason = "target"
        else:
            reason = "closed by hand"
        risk = self._risk(trade)
        closed = self.book.close_trade(trade_id, price, pnl, self.broker.now(), reason)
        text = self._closed_text(closed, risk)
        self.m.note_book(trade.symbol, "CLOSED", text)
        return text

    def amend(self, trade_id: int, sl: float | None, tp: float | None) -> str:
        trade = self.book.amend(trade_id, sl, tp, self.broker.now())
        d = self._digits(trade.symbol)
        return (
            f"#{trade.id} {WORD[trade.side]} {trade.symbol}: stop "
            f"{f'{trade.sl:.{d}f}' if trade.sl else 'none'}, target {f'{trade.tp:.{d}f}' if trade.tp else 'none'}."
        )

    def result(self, trade_id: int, pnl: float | None) -> str:
        trade = self.book.set_result(trade_id, pnl or 0.0)
        return f"#{trade.id} {trade.symbol}: result set to {self._money(trade.pnl or 0.0, True)}."

    def reopen(self, trade_id: int) -> str:
        trade = self.book.reopen(trade_id, self.broker.now())
        self._swept[trade.id] = self._last_bar(trade.symbol)  # do not close it again on the same bars
        return (
            f"#{trade.id} {WORD[trade.side]} {trade.symbol} is open again. Move its stop if yours has moved."
        )

    def delete(self, trade_id: int) -> str:
        trade = self.book.delete_trade(trade_id)
        return f"#{trade.id} {WORD[trade.side]} {trade.symbol} deleted from the book."

    def _risk(self, trade: BookTrade) -> float | None:
        """Account-currency loss at the stop the trade opened with: its 1R."""
        if not trade.initial_sl:
            return None
        loss = self.broker.value(trade.symbol, trade.side, trade.volume, trade.entry, trade.initial_sl)
        return -loss if loss < 0 else None

    def _closed_text(self, trade: BookTrade, risk: float | None) -> str:
        d = self._digits(trade.symbol)
        r = f" ({(trade.pnl or 0.0) / risk:+.2f}R)" if risk else ""
        return (
            f"#{trade.id} {WORD[trade.side]} {trade.volume:g} {trade.symbol} on {trade.account} closed at "
            f"{trade.exit_price:.{d}f} ({trade.exit_reason}): {self._money(trade.pnl or 0.0, True)}{r}."
        )

    # --- stops and targets hit while you were away ------------------------------------------------

    def _last_bar(self, symbol: str) -> Any:
        features = self.m.features(symbol)
        return None if features is None else features["time"].iloc[-1]

    def sweep(self, quotes: dict[str, tuple[float, float]]) -> None:
        """Close book trades whose stop or target the terminal's prices have crossed.

        Your broker closed the trade; nobody told the book. The result is worked out at the level
        and marked as assumed, because another broker's prices differ by a fraction of a pip.
        """
        for trade in self.book.trades(open_only=True):
            if not (trade.sl or trade.tp) or trade.symbol not in quotes:
                continue
            hit = self._crossed(trade, quotes[trade.symbol])
            if hit is None:
                continue
            reason, level, when = hit
            with self.broker.using(trade.account):
                risk = self._risk(trade)
            pnl = self.broker.value(trade.symbol, trade.side, trade.volume, trade.entry, level)
            closed = self.book.close_trade(trade.id, level, pnl, when, reason, assumed=True)
            text = (
                f"{self._closed_text(closed, risk)} Assumed from the terminal's prices: correct the result "
                f"if your broker shows another figure, or reopen it if it is still running."
            )
            self.m.note_book(trade.symbol, "CLOSED", text, send=True)
            self.m.book_changed()

    def _crossed(self, trade: BookTrade, quote: tuple[float, float]) -> tuple[str, float, datetime] | None:
        side = trade.side
        stop_word = "stop" if trade.sl == trade.initial_sl else "trailing stop"
        features = self.m.features(trade.symbol)
        if features is not None and self._swept.get(trade.id) != features["time"].iloc[-1]:
            self._swept[trade.id] = features["time"].iloc[-1]
            # Only bars that opened after the trade did, and after its levels were last moved: a
            # stop moved to breakeven must not be "hit" by a dip from before it was moved.
            since = max(trade.opened, trade.levels_set or trade.opened)
            bars = features[features["time"] >= pd.Timestamp(since)]
            spec = self.broker.symbol(trade.symbol)
            step = timedelta(minutes=self.m.config.bar_minutes)
            for bar in bars.itertuples(index=False):
                # bars are bid prices: a sell is closed at the ask, one spread higher
                extra = float(getattr(bar, "spread", 0) or 0) * spec.point if side < 0 else 0.0
                worst = bar.low if side > 0 else bar.high + extra
                best = bar.high if side > 0 else bar.low + extra
                when = (bar.time + step).to_pydatetime()
                if trade.sl and (worst - trade.sl) * side <= 0:
                    return stop_word, trade.sl, when  # both touched in one bar counts as the stop
                if trade.tp and (best - trade.tp) * side >= 0:
                    return "target", trade.tp, when
        price = quote[0] if side > 0 else quote[1]
        if trade.sl and (price - trade.sl) * side <= 0:
            return stop_word, trade.sl, self.broker.now()
        if trade.tp and (price - trade.tp) * side >= 0:
            return "target", trade.tp, self.broker.now()
        return None

    # --- what the page shows ----------------------------------------------------------------------

    def sizes(self, symbol: str, params: Any, row: pd.Series, tick: Any) -> dict[str, list[dict[str, Any]]]:
        """The same plan sized for every account that is not the selected one, per side."""
        key = (row["time"], self.book.version, self.broker.account_name())
        cached = self._sizes.get(symbol)
        if cached is None or cached[0] != key:
            out: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
            engine = Engine(self.m.config, self.broker, execute=False, live=True)
            for account in self.book.accounts():
                if account.name == self.broker.account_name():
                    continue
                with self.broker.using(account.name):
                    spec = self.broker.symbol(symbol)
                    equity = self.broker.account().equity
                    for name, side in (("buy", 1), ("sell", -1)):
                        plan = trade_plan(engine, symbol, spec, params, row, tick, side)
                        out[name].append({"account": account.name, "equity": equity, "volume": plan.volume,
                                          "risk_amount": plan.risk_amount, "risk_percent": plan.risk_percent,
                                          "rejected": plan.rejected})  # fmt: skip
            cached = (key, out)
            self._sizes[symbol] = cached
        return cached[1]

    def size_lines(self, symbol: str, side: int) -> list[str]:
        """For an alert: the lots at each of your accounts, when there is more than the selected one."""
        cached = self._sizes.get(symbol)
        if cached is None:
            return []
        lines = []
        for item in cached[1]["buy" if side > 0 else "sell"]:
            size = (
                f"{item['volume']:g} lots · risk {self._money(item['risk_amount'])}"
                if item["volume"]
                else f"no size ({item['rejected']})"
            )
            lines.append(f"{item['account']} ({self._money(item['equity'])}): {size}")
        return lines

    def state(self, now: datetime) -> dict[str, Any]:
        active = self.broker.account_name()
        accounts = []
        for account in self.book.accounts():
            floating = self.broker.floating_total(account.name)
            accounts.append({"name": account.name, "balance": account.balance,
                             "equity": account.balance + floating, "floating": floating,
                             "min_lot": account.min_lot, "lot_step": account.lot_step,
                             "leverage": account.leverage,
                             "open": len(self.book.trades(account.name, open_only=True)),
                             "updated": account.updated})  # fmt: skip
        return {"active": active, "accounts": accounts, **self._review(active, now)}

    def _review(self, active: str | None, now: datetime) -> dict[str, Any]:
        """Closed trades of the account in use and how they did in R.

        One of your accounts: the trades you logged, recomputed when the book changes. The
        MetaTrader 5 account: every trade on it, the ones placed on the phone included, read from
        the terminal's history once a minute. Nothing has to be logged by hand for that account.
        """
        key = (self.book.version, active, now.date())
        if active is None and time.monotonic() - self._mt5_read > MT5_REVIEW_SECONDS:
            self._history = None
        if self._history is None or self._history[0] != key:
            out: dict[str, Any] = {"closed": [], "review": None}
            if active is None:
                self._mt5_read = time.monotonic()
                out = self._mt5_review(now)
            else:
                since = now - timedelta(days=REVIEW_DAYS)
                recent = [t for t in self.book.trades(active, since=since) if not t.is_open]
                with self.broker.using(active):
                    risks = {t.id: self._risk(t) for t in recent[-15:]}
                    reviewed = closed_trades(self.broker, self.m.config.magic, since)
                out["closed"] = [
                    {"id": t.id, "symbol": t.symbol, "side": t.side, "volume": t.volume, "entry": t.entry,
                     "exit_price": t.exit_price, "pnl": t.pnl, "exit_reason": t.exit_reason, "closed": t.closed,
                     "assumed": t.assumed, "source": t.source,
                     "r": (t.pnl or 0.0) / risks[t.id] if risks.get(t.id) else None}
                    for t in reversed(recent[-15:])
                ]  # fmt: skip
                groups = (("all", reviewed), ("signal", [t for t in reviewed if not t.manual]),
                          ("own", [t for t in reviewed if t.manual]))  # fmt: skip
                out["review"] = {"days": REVIEW_DAYS, "verdict": verdict(summarize(reviewed))} | {
                    name: {"trades": s.trades, "win_rate": s.win_rate, "net": s.net, "avg_r": s.avg_r,
                           "measured": s.measured, "profit_factor": s.profit_factor}
                    for name, s in ((name, summarize(group)) for name, group in groups)
                }  # fmt: skip
            self._history = (key, out)
        return self._history[1]

    def _mt5_review(self, now: datetime) -> dict[str, Any]:
        config = self.m.config
        since = now - timedelta(days=REVIEW_DAYS)
        scope = None if config.risk.account_trades else config.magic
        try:
            reviewed = closed_trades(self.broker, scope, since, bot_magic=config.magic)
        except Exception:
            log.debug("closed trades not read", exc_info=True)
            return {"closed": [], "review": None}
        closed = [
            {"id": t.ticket, "symbol": t.symbol, "side": t.side, "volume": t.volume, "entry": t.entry,
             "exit_price": t.exit_price, "pnl": t.pnl, "exit_reason": t.exit, "closed": t.closed,
             "assumed": False, "source": OWN_CALL if t.manual else "bot", "r": t.r}
            for t in reversed(reviewed[-15:])
        ]  # fmt: skip
        groups = (("all", reviewed), ("signal", [t for t in reviewed if not t.manual]),
                  ("own", [t for t in reviewed if t.manual]))  # fmt: skip
        review = {"days": REVIEW_DAYS, "verdict": verdict(summarize(reviewed))} | {
            name: {"trades": s.trades, "win_rate": s.win_rate, "net": s.net, "avg_r": s.avg_r,
                   "measured": s.measured, "profit_factor": s.profit_factor}
            for name, s in ((name, summarize(group)) for name, group in groups)
        }  # fmt: skip
        return {"closed": closed, "review": review}

    # --- commands from the phone ------------------------------------------------------------------

    def command(self, text: str) -> str:
        """One Telegram message. Commands can log and correct trades; none can place an order."""
        words = text.strip().split()
        if not words or not words[0].startswith("/"):
            return ""
        name = words[0][1:].split("@")[0].lower()
        args = [w for w in words[1:] if "=" not in w]
        named = dict(w.split("=", 1) for w in words[1:] if "=" in w)
        try:
            reply = self._command(name, args, {k.lower(): v for k, v in named.items()})
        except (BookError, ValueError, IndexError, KeyError) as exc:
            reply = f"{exc or 'that did not work'}\nSend /help for the commands."
        finally:
            self.m.book_changed()
        return reply

    def _command(self, name: str, args: list[str], named: dict[str, str]) -> str:
        if name in ("help", "start"):
            return HELP
        if name == "equity":
            if len(args) < 2:
                raise BookError("use: /equity NAME AMOUNT")
            amount = _number({"equity": args[-1].replace(",", "")}, "equity", True)
            leverage = _number({"leverage": named.get("lev") or named.get("leverage")}, "leverage")
            return self.set_equity(" ".join(args[:-1]), amount, keep_rules=True, leverage=leverage)
        if name == "use":
            return self.use(" ".join(args) or None)
        if name == "accounts":
            return self._accounts_text()
        if name == "took":
            if len(args) < 2:
                raise BookError(
                    "use: /took SYMBOL buy|sell [lots=0.02] [entry=..] [sl=..] [tp=..] [account=NAME]"
                )
            return self.log(
                args[0], _side(args[1]), _number(named, "lots"), _number(named, "entry"),
                _number(named, "sl"), _number(named, "tp"), named.get("account"), named.get("note", ""),
            )  # fmt: skip
        if name == "close":
            price = _number({"price": args[1]}, "price") if len(args) > 1 else None
            return self.close(int(args[0].lstrip("#")), price, _number(named, "pnl"))
        if name in ("sl", "tp"):
            trade = self.book.trade(int(args[0].lstrip("#")))
            level = _number({"price": args[1]}, "price", True)
            return self.amend(
                trade.id, level if name == "sl" else trade.sl, level if name == "tp" else trade.tp
            )
        if name == "open":
            return self._open_text()
        if name == "status":
            return self.m.status_text()
        if name == "plan":
            if len(args) < 2:
                raise BookError("use: /plan SYMBOL buy|sell")
            return self.m.plan_text(self.m.match_symbol(args[0]), _side(args[1]))
        if name == "market":
            return self.m.market_text()
        if name == "levels":
            return self.m.levels_text(self.m.match_symbol(args[0]))
        if name == "calc":
            if len(args) < 2:
                raise BookError(
                    "use: /calc SYMBOL buy|sell [risk=1] [lots=] [sl=] [pips=] [tp=] [lev=] [equity=]"
                )
            body = {"symbol": args[0], "side": args[1], "risk_percent": named.get("risk"), "lots": named.get("lots"),
                    "stop": named.get("sl"), "stop_pips": named.get("pips"), "target": named.get("tp"),
                    "leverage": named.get("lev") or named.get("leverage"), "equity": named.get("equity"),
                    "risk_amount": named.get("money")}  # fmt: skip
            return calculator.summary(calc_request(self.m, body))
        if name == "news":
            return news_text(self.m, self.broker.now())
        raise BookError(f"unknown command /{name}")

    def _accounts_text(self) -> str:
        rows = self.state(self.broker.now())["accounts"]
        if not rows:
            return "No accounts yet. Add one with /equity NAME AMOUNT, for example: /equity Exness 500"
        active = self.broker.account_name()
        lines = [
            f"{'> ' if r['name'] == active else ''}{r['name']}: equity {self._money(r['equity'])}"
            + (f", {r['open']} open, floating {self._money(r['floating'], True)}" if r["open"] else "")
            for r in rows
        ]
        return "\n".join(
            [*lines, "" if active else "Using the MetaTrader 5 account. /use NAME to switch."]
        ).strip()

    def _open_text(self) -> str:
        trades = self.book.trades(open_only=True)
        if not trades:
            return "No open trades in the book."
        lines = []
        for t in trades:
            d = self._digits(t.symbol)
            with self.broker.using(t.account):
                risk, floating = self._risk(t), self.broker.floating(t)
            r = f" ({floating / risk:+.2f}R)" if risk else ""
            lines.append(
                f"#{t.id} {WORD[t.side]} {t.volume:g} {t.symbol} · {t.account} · from {t.entry:.{d}f} · "
                f"SL {f'{t.sl:.{d}f}' if t.sl else 'none'} · TP {f'{t.tp:.{d}f}' if t.tp else 'none'} · "
                f"{self._money(floating, True)}{r}"
            )
        return "\n".join(lines)


# --- the calculator --------------------------------------------------------------------------------

CALC_FIELDS = ("entry", "stop", "stop_pips", "target", "target_pips", "lots", "risk_percent", "risk_amount",
               "equity", "leverage", "margin_cap")  # fmt: skip


def calc_request(m: Monitor, body: dict[str, Any]) -> dict[str, Any]:
    """The calculator, with the bot's own plan and limits filling in what was left out.

    No stop given: the plan's stop and target for that side, moved with the entry if one is given.
    Neither lots nor risk given: the config's risk per trade. No margin cap: the config's.
    """
    symbol = m.match_symbol(str(body.get("symbol", "")))
    side = _side(body.get("side", 1))
    values = {key: _number(body, key) for key in CALC_FIELDS}
    params = m.config.for_symbol(symbol)
    from_plan = values["stop"] is None and values["stop_pips"] is None
    if from_plan:
        plan = m.advice_now(symbol).plans.get("buy" if side > 0 else "sell")
        if plan is not None:
            entry = values["entry"] or plan.entry
            values["stop"] = round(entry - side * plan.stop_distance, 10)
            if values["target"] is None and values["target_pips"] is None:
                values["target"] = round(entry + side * plan.target_distance, 10)
    if values["lots"] is None and values["risk_amount"] is None and values["risk_percent"] is None:
        values["risk_percent"] = params.risk.risk_percent
    if values["margin_cap"] is None:
        values["margin_cap"] = params.risk.max_margin_percent
    spec = m.broker.symbol(symbol)
    for key in ("stop", "target"):
        if values[key] is not None:
            values[key] = spec.round_price(values[key])
    try:
        result = calculator.calculate(m.broker, symbol, side, **values)
    except calculator.CalcError as exc:
        raise BookError(str(exc)) from None
    limit = params.risk.risk_percent
    if result["risk_percent"] is not None and result["risk_percent"] > limit + 1e-9:
        result["warnings"].append(
            f"At the stop this loses {result['risk_percent']:.2f}% of equity, more than the {limit:g}% a trade "
            f"in your config."
        )
    result["stops_from_plan"] = from_plan
    result["risk_percent_asked"] = values["risk_percent"]
    result["leverages"] = list(calculator.LEVERAGES)
    return result


# --- the news ---------------------------------------------------------------------------------------


def _currencies(specs: dict[str, Any]) -> dict[str, list[str]]:
    """Currency -> the watched instruments that carry it."""
    out: dict[str, list[str]] = {}
    for symbol, spec in specs.items():
        for asset, _ in market.legs(spec):
            out.setdefault(asset, []).append(symbol)
    return out


def news_view(m: Monitor, specs: dict[str, Any], now: datetime) -> dict[str, Any]:
    """What is coming for the watched currencies, and a warning before each high-impact event."""
    calendar, schedule = m.engine.news, m.config.schedule
    feed = getattr(m, "news_feed", None)
    status = {"on": bool(schedule.news_feed), "updated": None, "error": getattr(feed, "error", None)}
    if calendar is None:
        return {"events": [], **status}
    status["updated"] = calendar.updated()
    carried = _currencies(specs)
    events = []
    for e in calendar.upcoming(now, carried, hours=NEWS_HOURS):
        start = e.time - timedelta(minutes=schedule.news_before_minutes)
        end = e.time + timedelta(minutes=schedule.news_after_minutes)
        pauses = e.impact in ("high", "red")
        events.append({"time": e.time, "currency": e.currency, "impact": e.impact, "title": e.title,
                       "forecast": e.forecast, "previous": e.previous, "symbols": carried.get(e.currency, []),
                       "pauses": pauses, "paused_now": pauses and start <= now <= end,
                       "pause_from": start, "pause_to": end})  # fmt: skip
        if pauses and start <= now < e.time:
            _warn_news(m, e, carried.get(e.currency, []), start, end, now)
    return {"events": events, **status}


def _warn_news(
    m: Monitor, event: Any, symbols: list[str], start: datetime, end: datetime, now: datetime
) -> None:
    key = (event.time, event.currency, event.title)
    if key in m.news_seen:
        return
    m.news_seen.add(key)
    minutes = max(1, round((event.time - now).total_seconds() / 60))
    figures = ", ".join(f for f in (f"forecast {event.forecast}" if event.forecast else "",
                                     f"previous {event.previous}" if event.previous else "") if f)  # fmt: skip
    text = (
        f"{event.currency} {event.title} in {minutes} min ({event.time:%H:%M} UTC, high impact"
        + (f"; {figures}" if figures else "")
        + f"). New entries are paused on {', '.join(symbols)} from {start:%H:%M} to {end:%H:%M} UTC. "
        "Spreads widen and stops can fill worse around a release."
    )
    m.activity.append({"time": now, "symbol": event.currency, "action": "NEWS", "text": text})
    log.info("NEWS %s", text)
    if m.notifier:
        m.notifier.send(f"NEWS · {event.currency}\n{text}", "news")


def news_text(m: Monitor, now: datetime) -> str:
    view = (m.snapshot().get("market") or {}).get("news") or {}
    events = view.get("events") or []
    if not events:
        state = (
            "the feed is off (schedule.news_feed)" if not view.get("on") else "nothing high or medium impact"
        )
        return f"No news for your instruments in the next {NEWS_HOURS} hours: {state}."
    lines = []
    for e in events:
        when = pd.Timestamp(e["time"])
        figures = " · ".join(x for x in (f"f {e['forecast']}" if e["forecast"] else "",
                                          f"p {e['previous']}" if e["previous"] else "") if x)  # fmt: skip
        lines.append(f"{when:%a %H:%M} UTC · {e['currency']} {e['impact'].upper()} · {e['title']}"
                     + (f" ({figures})" if figures else "") + (" · ENTRIES PAUSED" if e["paused_now"] else ""))  # fmt: skip
    return "\n".join(lines)


# --- the market as a whole ------------------------------------------------------------------------


def market_view(
    m: Monitor, rows: list[dict[str, Any]], held: list[Any], equity: float, now: datetime
) -> dict:
    """Strength, correlation, exposure and levels, from the bars the advisor already holds."""
    features = {s: f for s in m.symbols if (f := m.features(s)) is not None}
    specs = {s: m.spec(s) for s in features if m.spec(s) is not None}
    key = tuple(f["time"].iloc[-1] for f in features.values())
    if m.market_cache is None or m.market_cache[0] != key:  # once per closed bar
        table = market.strength_table(features, specs, m.config.bar_minutes)
        slow = {
            "strength": table,
            "summary": market.summary(table),
            "correlation": market.correlation(features),
            "volatility": {s: market.volatility(f) for s, f in features.items()},
        }
        m.market_cache = (key, slow)
    slow = m.market_cache[1]
    quotes = {r["symbol"]: r for r in rows if "bid" in r}
    prices = {s: r["bid"] for s, r in quotes.items()}
    symbols = {}
    for symbol, row in quotes.items():
        last = features[symbol].iloc[-1] if symbol in features else None
        symbols[symbol] = {
            "levels": _levels(m, symbol, row["bid"], row["atr"]),
            "volatility": slow["volatility"].get(symbol),
            "overlap": {name: market.overlap(symbol, side, specs, held) for name, side in (("buy", 1), ("sell", -1))},
            "swap": m.swap(symbol),
            "rsi": None if last is None else float(last["rsi"]),
            "adx": None if last is None else float(last["adx"]),
        }  # fmt: skip
    return {
        "strength": slow["strength"],
        "summary": slow["summary"],
        "correlation": slow["correlation"],
        "exposure": market.exposure(held, specs, prices, equity),
        "warnings": market.correlated_positions(held, slow["correlation"]),
        "sessions": market.sessions(now),
        "symbols": symbols,
        "news": news_view(m, specs, now),
    }


def _levels(m: Monitor, symbol: str, price: float, atr: float) -> dict[str, Any] | None:
    cached = m.levels_cache.get(symbol)
    if cached and time.monotonic() - cached[0] < LEVELS_SECONDS:
        return cached[1]
    try:
        daily = _daily(m, symbol)
        forming = getattr(m.broker, "forming_bar", None)
        found = market.key_levels(daily, forming(symbol, "D1") if forming else None, price, atr)
    except Exception:
        log.debug("%s: levels not computed", symbol, exc_info=True)
        found = None
    m.levels_cache[symbol] = (time.monotonic(), found)
    return found


def _daily(m: Monitor, symbol: str) -> pd.DataFrame | None:
    """Closed daily bars: the trend filter's own when it reads D1, otherwise fetched now and then."""
    held = m.htf(symbol).get("D1")
    if held is not None and len(held) >= 2:
        return held.tail(40)
    cached = m.daily_cache.get(symbol)
    if cached is None or time.monotonic() - cached[0] > DAILY_SECONDS:
        cached = (time.monotonic(), m.broker.bars(symbol, "D1", 40))
        m.daily_cache[symbol] = cached
    return cached[1]
