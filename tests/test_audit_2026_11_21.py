"""Regressions from the whole-system audit of 21 Nov 2026."""

from __future__ import annotations

import argparse
from dataclasses import replace

import pandas as pd
import pytest

from tradebot import cli
from tradebot.broker.base import SymbolSpec
from tradebot.config import BotConfig, RiskParams


def test_a_symbol_takes_the_longest_matching_override():
    """Overrides match the broker's name by prefix: with "T" (AT&T) and "TSLA" both set, TSLAm
    must get TSLA's, whichever comes first in the file."""
    config = BotConfig(
        symbols=("T", "TSLA"),
        overrides={"T": {"risk": {"auto_trade": False}}, "TSLA": {"risk": {"risk_percent": 0.5}}},
    )
    assert config.for_symbol("TSLAm").risk == replace(RiskParams(), risk_percent=0.5)
    assert config.for_symbol("Tm").risk.auto_trade is False
    assert config.for_symbol("TSLA").risk.risk_percent == 0.5  # an exact name still wins


def test_a_trade_open_when_the_history_stops_leaves_at_the_last_price_seen(config, eurusd):
    """A stock's history had a three-month hole; a trade "held" through it exited at the first
    bar after it and booked +8R that nobody could have had."""
    from conftest import at
    from test_engine import flat

    from tradebot.backtest import run_backtest

    rows = [flat(0, signal=1, raw_signal=1), flat(15), flat(30, 1.1002)]
    after = pd.Timestamp(at(30)) + pd.Timedelta(days=90)
    rows.append({**flat(45, 1.1500), "time": after})  # far through the target, three months on
    rows.append({**flat(60, 1.1500), "time": after + pd.Timedelta(minutes=15)})
    bars = pd.DataFrame(rows).assign(exit_signal=0)
    result = run_backtest(config, {"EURUSD": bars}, {"EURUSD": eurusd}, features={"EURUSD": bars})
    (trade,) = result.trades
    assert trade.exit_reason == "data hole"
    assert trade.exit_price == 1.1002 and trade.exit_time < after


def test_a_weekend_is_not_a_data_hole(config, eurusd):
    from conftest import at
    from test_engine import flat

    from tradebot.backtest import run_backtest

    rows = [flat(0, signal=1, raw_signal=1), flat(15)]
    monday = pd.Timestamp(at(15)) + pd.Timedelta(days=2, hours=12)
    rows.append({**flat(30, 1.1001), "time": monday})
    bars = pd.DataFrame(rows).assign(exit_signal=0)
    result = run_backtest(config, {"EURUSD": bars}, {"EURUSD": eurusd}, features={"EURUSD": bars})
    assert [t.exit_reason for t in result.trades] == ["end of test"]


def test_swap_nights_follow_the_new_york_rollover_and_the_triple_day():
    from datetime import datetime

    from tradebot.broker.sim import rollovers

    # 21:00 UTC is 17:00 in New York in summer (EDT), 22:00 in winter (EST)
    assert rollovers(datetime(2026, 7, 6, 12), datetime(2026, 7, 6, 20), 2) == 0  # Monday, same day
    assert rollovers(datetime(2026, 7, 6, 12), datetime(2026, 7, 6, 21), 2) == 1  # through Monday's
    assert rollovers(datetime(2026, 1, 5, 12), datetime(2026, 1, 5, 21, 30), 2) == 0  # winter: 22:00
    assert rollovers(datetime(2026, 7, 8, 12), datetime(2026, 7, 9, 12), 2) == 3  # Wednesday: triple
    assert rollovers(datetime(2026, 7, 8, 12), datetime(2026, 7, 9, 12), 4) == 1  # a stock: not Wednesday
    assert rollovers(datetime(2026, 7, 10, 12), datetime(2026, 7, 13, 12), 4) == 3  # stock over a weekend
    assert rollovers(datetime(2026, 7, 10, 12), datetime(2026, 7, 13, 12), 2) == 1  # forex: Friday is one
    assert rollovers(datetime(2026, 7, 6, 21), datetime(2026, 7, 7, 12), 2) == 0  # opened at the rollover


def test_a_trade_held_overnight_pays_the_brokers_swap(config, eurusd):
    from conftest import at
    from test_engine import flat

    from tradebot.backtest import run_backtest

    costly = replace(config, backtest=replace(config.backtest, commission_per_lot=0.0, swap_long=-10.0))
    rows = [flat(0, signal=1, raw_signal=1), flat(15)]
    later = pd.Timestamp(at(15)) + pd.Timedelta(days=1)  # through one rollover (not a Wednesday)
    rows.append({**flat(30), "time": later})
    bars = pd.DataFrame(rows).assign(exit_signal=0)
    free = run_backtest(replace(costly, backtest=replace(costly.backtest, swap_long=0.0)), {"EURUSD": bars},
                        {"EURUSD": eurusd}, features={"EURUSD": bars})  # fmt: skip
    paid = run_backtest(costly, {"EURUSD": bars}, {"EURUSD": eurusd}, features={"EURUSD": bars})
    (with_swap,), (without,) = paid.trades, free.trades
    nights = 3 if pd.Timestamp(at(15)).day_of_week == 2 else 1
    assert with_swap.pnl == pytest.approx(without.pnl - 10.0 * with_swap.volume * nights)


def test_the_triple_swap_day_is_validated():
    from tradebot.config import ConfigError

    bad = BotConfig(backtest=replace(BotConfig().backtest, swap_triple_day=7))
    with pytest.raises(ConfigError, match="swap_triple_day"):
        bad.validate()


class _Broker:
    def resolve(self, name: str) -> str | None:
        return None if name == "GONE" else f"{name}m"

    def symbol(self, name: str) -> SymbolSpec:
        return SymbolSpec(name, 2, 0.01, 0.01, 1.0, 100, 0.01, 10, 0.01, 0, "USD", "USD")

    def shutdown(self) -> None:
        pass


def test_fetch_saves_each_symbols_history_under_its_own_name(tmp_path, monkeypatch):
    """A configured symbol the broker lacks used to shift every later one by a place: AMD's
    bars were written to META's file."""
    monkeypatch.setattr(cli, "_connect", lambda config: (_Broker(), None, ["AMDm", "METAm"]))

    def history(broker, symbol, timeframe, count):
        return pd.DataFrame({"time": [pd.Timestamp("2026-11-20 10:00")], "close": [float(len(symbol))],
                             "source": [symbol]})  # fmt: skip

    monkeypatch.setattr("tradebot.broker.mt5.fetch_history", history)
    config = BotConfig(symbols=("AMD", "GONE", "META"), timeframe="H1")
    args = argparse.Namespace(out=tmp_path, timeframes=["H1"], bars=10)
    assert cli.cmd_fetch(config, args) == 0
    assert pd.read_csv(tmp_path / "AMD_H1.csv")["source"].tolist() == ["AMDm"]
    assert pd.read_csv(tmp_path / "META_H1.csv")["source"].tolist() == ["METAm"]
    assert not (tmp_path / "GONE_H1.csv").exists()
