from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradebot.broker.sim import SimBroker
from tradebot.config import BacktestParams, BotConfig, ScheduleParams
from tradebot.data import preset_spec, synthetic_bars


@pytest.fixture(scope="session")
def bars() -> pd.DataFrame:
    return synthetic_bars(n=6000, seed=11)


@pytest.fixture
def config() -> BotConfig:
    """Permissive schedule so tests exercise trading logic, not the clock."""
    return BotConfig(
        symbols=("EURUSD",),
        timeframe="M15",
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )


@pytest.fixture
def eurusd():
    return preset_spec("EURUSD")


def make_sim(
    specs: dict, balance: float = 10_000.0, commission: float = 0.0, min_spread: float = 10.0
) -> SimBroker:
    params = {s: BacktestParams(commission_per_lot=commission, min_spread_points=min_spread) for s in specs}
    return SimBroker(specs, params, balance)


def bar(time: datetime, o: float, h: float, low: float, c: float, spread: float = 0, **extra) -> dict:
    row = {"time": pd.Timestamp(time), "open": o, "high": h, "low": low, "close": c, "spread": spread}
    row.update(
        {"atr": 0.0010, "signal": 0, "raw_signal": 0, "rsi": 50.0, "adx": 30.0, "trend": 1, "block": ""}
    )
    row.update(extra)
    return row


def feed(sim: SimBroker, symbol: str, row: dict, minutes: int = 15) -> datetime:
    close_time = row["time"] + pd.Timedelta(minutes=minutes)
    sim.start_bar(symbol, row, close_time)
    return close_time


T0 = datetime(2026, 3, 2, 10, 0)  # a Monday


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --- fake MetaTrader5 module ----------------------------------------------------------------------


class FakeMT5:
    """Just enough of the MetaTrader5 package to exercise the adapter without a terminal."""

    TIMEFRAME_M15, TIMEFRAME_H4 = 15, 240
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_RETCODE_DONE = 10009
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, server_offset_hours: float = 2.0, now: datetime = datetime(2026, 3, 4, 12, 7)) -> None:
        self.offset = timedelta(hours=server_offset_hours)
        self.utc_now = now
        self.sent: list[dict] = []
        self.retcodes: list[int] = []
        self.trade_mode = 0
        self.symbols = {
            "EURUSDm": SimpleNamespace(
                name="EURUSDm", visible=True, digits=5, point=1e-5, trade_tick_size=1e-5, trade_tick_value=1.0,
                trade_contract_size=100_000, volume_min=0.01, volume_max=100, volume_step=0.01, trade_stops_level=0,
                currency_base="EUR", currency_profit="USD", filling_mode=1,
            )
        }  # fmt: skip
        self.positions: list[SimpleNamespace] = []
        self.orders: list[SimpleNamespace] = []
        self.deals: list[SimpleNamespace] = []

    def server_epoch(self, utc: datetime) -> int:
        return int((utc + self.offset - datetime(1970, 1, 1)).total_seconds())

    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        return None

    def last_error(self):
        return (1, "Success")

    def account_info(self):
        return SimpleNamespace(balance=10_000.0, equity=10_000.0, margin=0.0, currency="USD", trade_mode=self.trade_mode,
                               leverage=100, server="Fake-Demo")  # fmt: skip

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True, connected=True)

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbols_get(self, group=None):
        return list(self.symbols.values())

    def symbol_select(self, name, enable):
        return True

    def symbol_info_tick(self, name):
        return SimpleNamespace(time=self.server_epoch(self.utc_now), bid=1.10000, ask=1.10012)

    def copy_rates_from_pos(self, name, timeframe, start, count):
        minutes = timeframe
        # newest bar is the forming one (opened before "now" but not closed yet)
        last_open = self.utc_now - timedelta(
            minutes=self.utc_now.minute % minutes, seconds=self.utc_now.second
        )
        times = [last_open - timedelta(minutes=minutes * i) for i in range(count)][::-1]
        n = len(times)
        close = 1.1 + np.cumsum(np.full(n, 0.0001))
        dtype = [
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "i8"),
            ("spread", "i4"),
            ("real_volume", "i8"),
        ]
        rows = [
            (self.server_epoch(t), c - 0.0001, c + 0.0002, c - 0.0002, c, 100, 8, 0)
            for t, c in zip(times, close)
        ]
        return np.array(rows, dtype=dtype)

    def positions_get(self):
        return self.positions

    def history_orders_get(self, position=None):
        return [o for o in self.orders if o.position_id == position]

    def history_deals_get(self, start, end):
        return [d for d in self.deals if start <= d.time <= end]

    def order_send(self, request):
        self.sent.append(request)
        code = self.retcodes.pop(0) if self.retcodes else self.TRADE_RETCODE_DONE
        return SimpleNamespace(
            retcode=code,
            order=777,
            price=request.get("price", 0),
            comment="done" if code == 10009 else "invalid fill",
        )

    def order_calc_profit(self, kind, symbol, volume, price_open, price_close):
        info = self.symbols[symbol]
        return round((price_close - price_open) * info.trade_contract_size * volume, 2)

    def order_calc_margin(self, kind, symbol, volume, price):
        return volume * 100_000 * price / 100


@pytest.fixture
def fake_mt5() -> FakeMT5:
    return FakeMT5()


def with_schedule(config: BotConfig, **changes) -> BotConfig:
    return replace(config, schedule=replace(config.schedule, **changes))
