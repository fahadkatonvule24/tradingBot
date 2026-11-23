"""Your own accounts and trades, for trading done by hand at other brokers.

The MetaTrader 5 terminal gives the bot prices. It does not know the account you actually trade:
its equity, the trades you place on your phone, or what they made and lost. The book holds that.
You type in an account's equity and log each trade; ``BookBroker`` then answers the engine's
questions (how much equity, what is open, what closed today) from the book instead of from the
terminal. Nothing else changes: the sizing, the loss limits, the reversal warnings and the review
are the same code, now working on your real account.

Amounts are in the terminal account's currency, because that is the currency its profit
calculator answers in. Keep the terminal on an account in the same currency as yours.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from tradebot.broker.base import Account, Deal, OrderRequest, OrderResult, Position, SymbolSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_accounts (
    name        TEXT PRIMARY KEY,
    balance     REAL NOT NULL,
    min_lot     REAL,
    lot_step    REAL,
    leverage    REAL,
    updated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account     TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        INTEGER NOT NULL,
    volume      REAL NOT NULL,
    entry       REAL NOT NULL,
    sl          REAL,
    tp          REAL,
    initial_sl  REAL,
    opened_utc  TEXT NOT NULL,
    closed_utc  TEXT,
    exit_price  REAL,
    pnl         REAL,
    exit_reason TEXT,
    assumed     INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'own call',
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS book_trades_account ON book_trades(account, closed_utc);
CREATE TABLE IF NOT EXISTS book_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen_stops (ticket INTEGER PRIMARY KEY, sl REAL NOT NULL);
CREATE TABLE IF NOT EXISTS account_seen_stops (
    login INTEGER NOT NULL, server TEXT NOT NULL, ticket INTEGER NOT NULL, sl REAL NOT NULL,
    PRIMARY KEY (login, server, ticket)
);
"""

SIGNAL, OWN_CALL = "signal", "own call"
MAX_NAME = 24


class BookError(ValueError):
    """A request the book cannot carry out, worded for the person who made it."""


@dataclass(frozen=True)
class BookAccount:
    name: str
    balance: float
    """Closed-trade balance: the equity you entered, moved by every trade closed since."""
    min_lot: float | None
    lot_step: float | None
    updated: datetime
    leverage: float | None = None
    """1:N at this broker. Set, it checks margin and caps the size by it; empty, margin is not checked."""


@dataclass(frozen=True)
class BookTrade:
    id: int
    account: str
    symbol: str
    side: int
    volume: float
    entry: float
    sl: float | None
    tp: float | None
    initial_sl: float | None
    opened: datetime
    closed: datetime | None = None
    exit_price: float | None = None
    pnl: float | None = None
    exit_reason: str = ""
    assumed: bool = False
    """Closed by the dashboard because price crossed the stop or target, not confirmed by you."""
    source: str = OWN_CALL
    """``signal`` if the bot was signalling this side when it was logged, else ``own call``."""
    note: str = ""
    levels_set: datetime | None = None
    """When the stop or target was last moved (or the trade reopened); None if never. Price history
    before it was judged against the old levels, so the sweep must not test it against the new."""

    @property
    def is_open(self) -> bool:
        return self.closed is None


_COLUMNS = (
    "id, account, symbol, side, volume, entry, sl, tp, initial_sl, opened_utc, closed_utc, "
    "exit_price, pnl, exit_reason, assumed, source, note, levels_utc"
)


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _trade(row: tuple) -> BookTrade:
    return BookTrade(
        id=row[0], account=row[1], symbol=row[2], side=row[3], volume=row[4], entry=row[5], sl=row[6],
        tp=row[7], initial_sl=row[8], opened=datetime.fromisoformat(row[9]), closed=_when(row[10]),
        exit_price=row[11], pnl=row[12], exit_reason=row[13] or "", assumed=bool(row[14]),
        source=row[15], note=row[16], levels_set=_when(row[17]),
    )  # fmt: skip


def check_levels(side: int, entry: float, sl: float | None, tp: float | None) -> None:
    """A stop belongs on the losing side of the entry and a target on the winning side."""
    if side not in (1, -1):
        raise BookError("side must be buy or sell")
    if not entry or entry <= 0:
        raise BookError("the entry price must be positive")
    word = "buy" if side > 0 else "sell"
    if sl is not None and (sl <= 0 or (sl - entry) * side >= 0):
        raise BookError(f"the stop of a {word} must be {'below' if side > 0 else 'above'} the entry")
    if tp is not None and (tp <= 0 or (tp - entry) * side <= 0):
        raise BookError(f"the target of a {word} must be {'above' if side > 0 else 'below'} the entry")


class Book:
    """Accounts and trades in the journal's SQLite file. Safe to call from any thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.RLock()
        self._db.executescript(SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(book_accounts)")}
        if "leverage" not in columns:  # a book made before leverage was added
            self._db.execute("ALTER TABLE book_accounts ADD COLUMN leverage REAL")
            self._db.commit()
        trade_columns = {row[1] for row in self._db.execute("PRAGMA table_info(book_trades)")}
        if "levels_utc" not in trade_columns:  # a book made before amended stops were dated
            self._db.execute("ALTER TABLE book_trades ADD COLUMN levels_utc TEXT")
            self._db.commit()
        self.version = 0
        """Goes up on every change, so anything computed from the book knows when to recompute."""

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cursor = self._db.execute(sql, args)
        self._db.commit()
        self.version += 1
        return cursor

    # --- accounts ---------------------------------------------------------------------------------

    def accounts(self) -> list[BookAccount]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name, balance, min_lot, lot_step, updated_utc, leverage FROM book_accounts ORDER BY name"
            ).fetchall()
        return [BookAccount(r[0], r[1], r[2], r[3], datetime.fromisoformat(r[4]), r[5]) for r in rows]

    def account(self, name: str | None) -> BookAccount | None:
        return next((a for a in self.accounts() if name and a.name.lower() == name.lower()), None)

    def save_account(
        self,
        name: str,
        balance: float,
        now: datetime,
        min_lot: float | None = None,
        lot_step: float | None = None,
        leverage: float | None = None,
    ) -> BookAccount:
        """Add an account, or set the balance (and lot rules and leverage) of one that exists."""
        name = " ".join(str(name).split())
        if not name or len(name) > MAX_NAME:
            raise BookError(f"the account needs a name of 1 to {MAX_NAME} characters")
        if not balance > 0:
            raise BookError("the equity must be a positive amount")
        for label, value in (("minimum lot", min_lot), ("lot step", lot_step)):
            if value is not None and not value > 0:
                raise BookError(f"the {label} must be positive, or left empty to use the terminal's")
        if leverage is not None and leverage < 1:
            raise BookError("the leverage is the N of 1:N (1 or more), or empty to leave margin unchecked")
        existing = self.account(name)
        with self._lock:
            if existing:
                name = existing.name
                self._write(
                    "UPDATE book_accounts SET balance = ?, min_lot = ?, lot_step = ?, leverage = ?, updated_utc = ? "
                    "WHERE name = ?",
                    (balance, min_lot, lot_step, leverage, now.isoformat(timespec="seconds"), name),
                )
            else:
                self._write(
                    "INSERT INTO book_accounts (name, balance, min_lot, lot_step, leverage, updated_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, balance, min_lot, lot_step, leverage, now.isoformat(timespec="seconds")),
                )
        return self.account(name)  # type: ignore[return-value]

    def seen_stop(self, ticket: int, account: tuple[int, str] | None = None) -> float | None:
        """First stop for this account and ticket. Legacy unscoped stops are kept separately:
        their account is unknown, so they cannot safely be assigned to a connected terminal.
        Read through to SQLite so the dashboard and trading loop see each other's first stop.
        """
        with self._lock:
            if account is None:
                row = self._db.execute("SELECT sl FROM seen_stops WHERE ticket = ?", (ticket,)).fetchone()
            else:
                row = self._db.execute(
                    "SELECT sl FROM account_seen_stops WHERE login = ? AND server = ? AND ticket = ?",
                    (*account, ticket),
                ).fetchone()
            return row[0] if row else None

    def claim_legacy_stop(self, ticket: int, account: tuple[int, str]) -> float | None:
        """Move a first stop saved before stops were kept per account to ``account``. Only for a
        ticket from that account's own positions or deal history, which proves whose it is. Moved,
        not copied, so no second account can ever take it. None when there is none to move."""
        with self._lock:
            row = self._db.execute("SELECT sl FROM seen_stops WHERE ticket = ?", (ticket,)).fetchone()
            if row is None:
                return None
            self._db.execute(
                "INSERT OR IGNORE INTO account_seen_stops (login, server, ticket, sl) VALUES (?, ?, ?, ?)",
                (*account, ticket, row[0]),
            )
            self._db.execute("DELETE FROM seen_stops WHERE ticket = ?", (ticket,))
            self._db.commit()
        return self.seen_stop(ticket, account)

    def first_stop(self, ticket: int, account: tuple[int, str] | None) -> float | None:
        """The first stop saved for ``ticket``, a ticket of ``account``'s own: its scoped one, else
        one saved before stops were kept per account (claimed for it now)."""
        if account is None:
            return self.seen_stop(ticket)
        found = self.seen_stop(ticket, account)
        return found if found is not None else self.claim_legacy_stop(ticket, account)

    def remember_stop(self, ticket: int, sl: float, account: tuple[int, str] | None = None) -> None:
        """Keep the first stop a position was seen with. Not a change to the book: no new version."""
        if not math.isfinite(sl) or sl <= 0 or self.seen_stop(ticket, account) is not None:
            return
        with self._lock:
            if account is None:
                self._db.execute("INSERT OR IGNORE INTO seen_stops (ticket, sl) VALUES (?, ?)", (ticket, sl))
            else:
                self._db.execute(
                    "INSERT OR IGNORE INTO account_seen_stops (login, server, ticket, sl) VALUES (?, ?, ?, ?)",
                    (*account, ticket, sl),
                )
            self._db.commit()

    def remove_account(self, name: str) -> None:
        account = self._need_account(name)
        if self.trades(account.name, open_only=True):
            raise BookError(f"{account.name} still has open trades in the book: close or delete them first")
        with self._lock:
            self._write("DELETE FROM book_accounts WHERE name = ?", (account.name,))
            if (self.active() or "").lower() == account.name.lower():
                self.use(None)

    def active(self) -> str | None:
        """The account every figure is computed for; None means the MetaTrader 5 account."""
        with self._lock:
            row = self._db.execute("SELECT value FROM book_settings WHERE key = 'active'").fetchone()
        account = self.account(row[0]) if row and row[0] else None
        return account.name if account else None

    def use(self, name: str | None) -> str | None:
        chosen = self._need_account(name).name if name else None
        with self._lock:
            self._write(
                "INSERT INTO book_settings (key, value) VALUES ('active', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (chosen or "",),
            )
        return chosen

    def _need_account(self, name: str | None) -> BookAccount:
        account = self.account(name)
        if account is None:
            known = ", ".join(a.name for a in self.accounts()) or "none yet"
            raise BookError(f"no account named {name!r} (accounts: {known})")
        return account

    def _move_balance(self, account: str, amount: float) -> None:
        self._write("UPDATE book_accounts SET balance = balance + ? WHERE name = ?", (amount, account))

    # --- trades -----------------------------------------------------------------------------------

    def trades(
        self, account: str | None = None, *, open_only: bool = False, since: datetime | None = None
    ) -> list[BookTrade]:
        """Trades oldest first. ``since`` keeps open trades and those closed at or after it."""
        sql, args = f"SELECT {_COLUMNS} FROM book_trades WHERE 1 = 1", []
        if account is not None:
            sql, args = sql + " AND account = ? COLLATE NOCASE", [account]
        if open_only:
            sql += " AND closed_utc IS NULL"
        if since is not None:
            sql += " AND (closed_utc IS NULL OR closed_utc >= ?)"
            args.append(since.isoformat(timespec="seconds"))
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY opened_utc, id", args).fetchall()
        return [_trade(r) for r in rows]

    def trade(self, trade_id: int) -> BookTrade:
        with self._lock:
            row = self._db.execute(f"SELECT {_COLUMNS} FROM book_trades WHERE id = ?", (trade_id,)).fetchone()
        if row is None:
            raise BookError(f"no trade #{trade_id} in the book")
        return _trade(row)

    def open_trade(
        self,
        account: str,
        symbol: str,
        side: int,
        volume: float,
        entry: float,
        now: datetime,
        sl: float | None = None,
        tp: float | None = None,
        source: str = OWN_CALL,
        note: str = "",
    ) -> BookTrade:
        owner = self._need_account(account)
        if not volume > 0:
            raise BookError("the lot size must be positive")
        sl, tp = sl or None, tp or None
        check_levels(side, entry, sl, tp)
        with self._lock:
            cursor = self._write(
                "INSERT INTO book_trades (account, symbol, side, volume, entry, sl, tp, initial_sl, opened_utc, "
                "source, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner.name, symbol, side, volume, entry, sl, tp, sl, now.isoformat(timespec="seconds"),
                 source, str(note)[:200]),
            )  # fmt: skip
            if cursor.lastrowid is None:  # sqlite gives the new row's id; None only if nothing was written
                raise BookError("the trade was not saved")
            return self.trade(cursor.lastrowid)

    def amend(
        self, trade_id: int, sl: float | None, tp: float | None, now: datetime | None = None
    ) -> BookTrade:
        """Move the stop or target of an open trade. The stop it opened with still defines 1R.
        ``now`` dates the change, so bars before it are not tested against the new levels."""
        trade = self._need_open(trade_id)
        sl, tp = sl or None, tp or None
        for label, level in (("stop", sl), ("target", tp)):
            if level is not None and level <= 0:
                raise BookError(f"the {label} must be a positive price")
        if tp is not None and (tp - trade.entry) * trade.side <= 0:
            raise BookError("the target must be on the winning side of the entry")
        with self._lock:
            self._write(
                "UPDATE book_trades SET sl = ?, tp = ?, initial_sl = COALESCE(initial_sl, ?), "
                "levels_utc = COALESCE(?, levels_utc) WHERE id = ?",
                (
                    sl,
                    tp,
                    sl if sl is not None and (sl - trade.entry) * trade.side < 0 else None,
                    now.isoformat(timespec="seconds") if now else None,
                    trade_id,
                ),
            )
        return self.trade(trade_id)

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl: float,
        now: datetime,
        reason: str = "closed by hand",
        assumed: bool = False,
    ) -> BookTrade:
        trade = self._need_open(trade_id)
        if not exit_price > 0:
            raise BookError("the exit price must be positive")
        with self._lock:
            self._write(
                "UPDATE book_trades SET closed_utc = ?, exit_price = ?, pnl = ?, exit_reason = ?, assumed = ? "
                "WHERE id = ?",
                (max(now, trade.opened).isoformat(timespec="seconds"), exit_price, pnl, reason, int(assumed),
                 trade_id),
            )  # fmt: skip
            self._move_balance(trade.account, pnl)
        return self.trade(trade_id)

    def set_result(self, trade_id: int, pnl: float) -> BookTrade:
        """Replace a closed trade's result with the figure your broker shows."""
        trade = self.trade(trade_id)
        if trade.is_open:
            raise BookError(f"trade #{trade_id} is still open: close it first")
        with self._lock:
            self._write("UPDATE book_trades SET pnl = ?, assumed = 0 WHERE id = ?", (pnl, trade_id))
            self._move_balance(trade.account, pnl - (trade.pnl or 0.0))
        return self.trade(trade_id)

    def reopen(self, trade_id: int, now: datetime | None = None) -> BookTrade:
        """Undo a close, for a trade the dashboard assumed was stopped out and was not. ``now``
        dates the reopening, so the bars that seemed to close it are not tested again."""
        trade = self.trade(trade_id)
        if trade.is_open:
            raise BookError(f"trade #{trade_id} is already open")
        with self._lock:
            self._write(
                "UPDATE book_trades SET closed_utc = NULL, exit_price = NULL, pnl = NULL, exit_reason = NULL, "
                "assumed = 0, levels_utc = COALESCE(?, levels_utc) WHERE id = ?",
                (now.isoformat(timespec="seconds") if now else None, trade_id),
            )
            self._move_balance(trade.account, -(trade.pnl or 0.0))
        return self.trade(trade_id)

    def delete_trade(self, trade_id: int) -> BookTrade:
        trade = self.trade(trade_id)
        with self._lock:
            self._write("DELETE FROM book_trades WHERE id = ?", (trade_id,))
            if not trade.is_open:
                self._move_balance(trade.account, -(trade.pnl or 0.0))
        return trade

    def _need_open(self, trade_id: int) -> BookTrade:
        trade = self.trade(trade_id)
        if not trade.is_open:
            raise BookError(f"trade #{trade_id} is already closed")
        return trade


REFUSAL = "one of your own accounts is selected: place the order at your broker, then log it here"


class BookBroker:
    """The engine's ``Broker``. Prices always come from ``inner`` (the terminal); the account, the
    open positions and the deal history come from the book while one of your accounts is selected."""

    def __init__(self, inner: Any, book: Book, magic: int) -> None:
        self.inner, self.book, self.magic = inner, book, magic
        self._using: str | None = None

    def __getattr__(self, name: str) -> Any:  # prices, bars, clock, connection: the terminal's
        return getattr(self.inner, name)

    def account_name(self) -> str | None:
        return self._using or self.book.active()

    @property
    def advisory_only(self) -> bool:
        """A named account supplies sizing estimates; this adapter refuses all its orders."""
        return self.account_name() is not None

    @contextmanager
    def using(self, name: str) -> Iterator[None]:
        """Answer for ``name`` inside the block, whichever account is selected: sizes for them all."""
        previous, self._using = self._using, name
        try:
            yield
        finally:
            self._using = previous

    # --- money ------------------------------------------------------------------------------------

    def value(self, symbol: str, side: int, volume: float, entry: float, price: float) -> float:
        """Account-currency result of ``volume`` lots from ``entry`` to ``price``."""
        calc = getattr(self.inner, "calc_profit", None)
        if calc is not None:
            result = calc(symbol, side, volume, entry, price)
            if result is not None:
                return float(result)
        spec = self.inner.symbol(symbol)
        return (price - entry) * side / spec.tick_size * spec.tick_value * volume

    def exit_price(self, trade: BookTrade) -> float:
        tick = self.inner.tick(trade.symbol)
        return tick.bid if trade.side > 0 else tick.ask

    def floating(self, trade: BookTrade) -> float:
        try:
            return self.value(trade.symbol, trade.side, trade.volume, trade.entry, self.exit_price(trade))
        except Exception:  # a symbol the terminal no longer quotes must not take the account down
            return 0.0

    def floating_total(self, account: str) -> float:
        return sum(self.floating(t) for t in self.book.trades(account, open_only=True))

    def notional(self, symbol: str, volume: float, price: float) -> float:
        """Account-currency value of what ``volume`` lots control at ``price``."""
        spec = self.inner.symbol(symbol)
        return volume * price / spec.tick_size * spec.tick_value

    def used_margin(self, account: str, leverage: float | None) -> float:
        if not leverage:
            return 0.0
        return sum(
            self.notional(t.symbol, t.volume, t.entry) / leverage
            for t in self.book.trades(account, open_only=True)
        )

    # --- Broker interface -------------------------------------------------------------------------

    def account(self) -> Account:
        own = self.book.account(self.account_name())
        base = self.inner.account()
        if own is None:
            return base
        return Account(
            own.balance, own.balance + self.floating_total(own.name), self.used_margin(own.name, own.leverage),
            base.currency, False, int(own.leverage or base.leverage),
        )  # fmt: skip

    def symbol(self, name: str) -> SymbolSpec:
        spec = self.inner.symbol(name)
        own = self.book.account(self.account_name())
        if own is None or (own.min_lot is None and own.lot_step is None):
            return spec
        return replace(
            spec, volume_min=own.min_lot or spec.volume_min, volume_step=own.lot_step or spec.volume_step
        )

    def terminal_positions(self, magic: int | None) -> list[Position]:
        """The terminal's open positions, whichever account is selected here, each with its first
        stop as 1R: a phone trade often gets its stop after it opened."""
        account = getattr(self.inner, "account_id", lambda: None)()
        held = self.inner.positions(magic)
        opening = getattr(self.inner, "opening", None)
        for p in held:
            if account is not None:
                # a trade open across the upgrade keeps the first stop saved for it then, not the
                # stop it has now (breakeven may have moved it already)
                self.book.first_stop(p.ticket, account)
            self.book.remember_stop(p.ticket, p.sl, account)
            first = self.book.seen_stop(p.ticket, account)
            if first and opening is not None and not opening(p.ticket)[1]:
                p.initial_sl = first
        return held

    def positions(self, magic: int | None) -> list[Position]:
        name = self.account_name()
        if name is None:
            return self.terminal_positions(magic)
        return [
            Position(
                ticket=t.id, symbol=t.symbol, side=t.side, volume=t.volume, price_open=t.entry,
                sl=t.sl or 0.0, tp=t.tp or 0.0, time_open=t.opened, initial_volume=t.volume,
                initial_sl=t.initial_sl, profit=self.floating(t), comment=t.source, magic=self.magic,
            )
            for t in self.book.trades(name, open_only=True)
        ]  # fmt: skip

    def deals(self, since: datetime, magic: int | None) -> list[Deal]:
        name = self.account_name()
        if name is None:
            return self.inner.deals(since, magic)
        out = []
        for t in self.book.trades(name):
            comment = "manual" if t.source == OWN_CALL else "signal"
            if t.opened >= since:
                out.append(
                    Deal(2 * t.id, t.id, t.symbol, t.opened, t.side, t.volume, t.entry, 0.0, "in", comment)
                )
            if t.closed is not None and t.closed >= since:
                out.append(Deal(2 * t.id + 1, t.id, t.symbol, t.closed, -t.side, t.volume,
                                t.exit_price or t.entry, t.pnl or 0.0, "out", t.exit_reason))  # fmt: skip
        return sorted(out, key=lambda d: d.time)

    def opening(self, ticket: int) -> tuple[float | None, float | None]:
        """(volume, stop) a position opened with: its 1R, for the review."""
        if self.account_name() is None:
            account = getattr(self.inner, "account_id", lambda: None)()
            inner = getattr(self.inner, "opening", None)
            volume, stop = inner(ticket) if inner else (None, None)
            return volume, stop or self.book.first_stop(ticket, account)
        try:
            trade = self.book.trade(ticket)
        except BookError:
            return None, None
        return trade.volume, trade.initial_sl

    def margin_required(self, symbol: str, side: int, volume: float) -> float | None:
        name = self.account_name()
        if name is None:
            return self.inner.margin_required(symbol, side, volume)
        own = self.book.account(name)
        if own is None or not own.leverage:
            return None  # no leverage given for this account, so margin is not checked
        tick = self.inner.tick(symbol)
        return self.notional(symbol, volume, tick.ask if side > 0 else tick.bid) / own.leverage

    def open(self, request: OrderRequest, magic: int) -> OrderResult:
        if self.account_name() is None:
            return self.inner.open(request, magic)
        return OrderResult(False, message=REFUSAL)

    def close(self, position: Position, volume: float | None, magic: int, comment: str) -> OrderResult:
        if self.account_name() is None:
            return self.inner.close(position, volume, magic, comment)
        return OrderResult(False, message=REFUSAL)

    def modify(self, position: Position, sl: float, tp: float, magic: int) -> OrderResult:
        if self.account_name() is None:
            return self.inner.modify(position, sl, tp, magic)
        return OrderResult(False, message=REFUSAL)
