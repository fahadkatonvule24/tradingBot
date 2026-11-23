"""Adverse cases from the system audit of 23 November 2026; no live services."""

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from test_track import printing

from tradebot.book import Book, BookBroker
from tradebot.broker.mt5 import MT5Broker, MT5Error
from tradebot.config import BotConfig, ConfigError, MT5Login, config_from_dict
from tradebot.data import load_spec, preset_spec, synthetic_bars
from tradebot.optimize import apply_params, walk_forward
from tradebot.ui.track import TrackRecords


def test_initial_stops_are_isolated_by_login_and_server(fake_mt5, tmp_path):
    login, server = [101], ["Broker-A"]
    account = fake_mt5.account_info
    fake_mt5.account_info = lambda: SimpleNamespace(
        **(vars(account()) | {"login": login[0], "server": server[0]})
    )
    fake_mt5.positions = [SimpleNamespace(ticket=41, magic=0, symbol="EURUSDm", type=0, volume=0.1,
                                          price_open=1.1, sl=1.09, tp=1.12,
                                          time=fake_mt5.server_epoch(datetime(2026, 3, 4, 10)),
                                          profit=0.0, comment="")]  # fmt: skip
    fake_mt5.orders = [SimpleNamespace(position_id=41, time_setup=0, volume_initial=0.1, sl=0.0)]
    broker = MT5Broker(MT5Login(), offset_hours=2, module=fake_mt5)
    book = Book(tmp_path / "book.sqlite")
    wrapped = BookBroker(broker, book, 1)
    try:
        assert wrapped.terminal_positions(None)[0].initial_sl == 1.09
        login[0] = 202
        fake_mt5.positions[0].sl = 1.08
        assert wrapped.terminal_positions(None)[0].initial_sl == 1.08
        server[0] = "Broker-B"
        fake_mt5.positions[0].sl = 1.07
        assert wrapped.terminal_positions(None)[0].initial_sl == 1.07
        login[0], server[0] = 101, "Broker-A"
        fake_mt5.positions[0].sl = 1.101  # breakeven moved, but this account's original stop remains
        assert wrapped.terminal_positions(None)[0].initial_sl == 1.09
        assert wrapped.opening(41)[1] == 1.09
    finally:
        book.close()


def test_two_book_connections_keep_the_same_first_stop(tmp_path):
    first, second = Book(tmp_path / "book.sqlite"), Book(tmp_path / "book.sqlite")
    try:
        assert second.seen_stop(41) is None  # the second process checked before a stop existed
        first.remember_stop(41, 1.09)
        second.remember_stop(41, 1.101)  # a later breakeven stop must not replace 1R in memory
        assert second.seen_stop(41) == first.seen_stop(41) == 1.09
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"rsi_buy_max": 101}, "rsi_buy_max"),
        ({"rsi_sell_min": -1}, "rsi_sell_min"),
        ({"pullback_buy_below": -1}, "pullback_buy_below"),
        ({"pullback_sell_above": 101}, "pullback_sell_above"),
        ({"adx_min": 101}, "adx_min"),
        ({"sr_min_atr": -1}, "sr_min_atr"),
        ({"fib_tolerance_atr": -1}, "fib_tolerance_atr"),
    ],
)
def test_impossible_indicator_settings_are_rejected(values, field):
    with pytest.raises(ConfigError, match=field):
        config_from_dict({"strategy": values}, env={})


def test_history_must_cover_the_indicators_used_by_every_symbol():
    raw = {"history_bars": 200, "strategy": {"slow_ema": 500}}
    with pytest.raises(ConfigError, match="history_bars"):
        config_from_dict(raw, env={})


@pytest.mark.parametrize("symbols", [["EURUSD", "EURUSD"], ["EURUSD", "eurusd"], [" EURUSD"]])
def test_duplicate_or_padded_symbols_are_rejected(symbols):
    with pytest.raises(ConfigError, match="symbols"):
        config_from_dict({"symbols": symbols}, env={})


def test_optimizer_does_not_report_a_winner_that_fails_minimum_trades():
    config = BotConfig().with_strategy(use_trend_filter=False)
    data = {"EURUSD": synthetic_bars(n=400, seed=19)}
    with pytest.raises(ValueError, match=r"no qualifying.*min_trades"):
        walk_forward(config, data, {"EURUSD": preset_spec("EURUSD")}, grid={"risk.tp_atr": [3.0]},
                     folds=1, min_trades=100_000, jobs=1)  # fmt: skip


@pytest.mark.parametrize("kwargs, message", [({"folds": 0}, "folds"), ({"jobs": -1}, "jobs"),
                                           ({"min_trades": -1}, "min_trades"),
                                           ({"grid": {"risk.tp_atr": []}}, "grid")])  # fmt: skip
def test_bad_optimizer_inputs_are_rejected_early(kwargs, message):
    data = {"EURUSD": synthetic_bars(n=400)}
    options = {"folds": 1, "jobs": 1, "min_trades": 1, "grid": {"risk.tp_atr": [3.0]}} | kwargs
    with pytest.raises(ValueError, match=message):
        walk_forward(BotConfig(), data, {"EURUSD": preset_spec("EURUSD")}, **options)


def test_optimized_parameters_apply_even_with_a_symbol_override():
    config = BotConfig(
        overrides={"EURUSD": {"strategy": {"fast_ema": 15}, "risk": {"tp_atr": 3.0, "risk_percent": 0.5}}}
    )
    tuned = apply_params(config, {"strategy.fast_ema": 8, "risk.tp_atr": 4.5})
    assert tuned.for_symbol("EURUSDm").strategy.fast_ema == 8
    assert tuned.for_symbol("EURUSDm").risk.tp_atr == 4.5
    assert tuned.for_symbol("EURUSDm").risk.risk_percent == 0.5
    assert config.for_symbol("EURUSD").risk.tp_atr == 3.0


@pytest.mark.parametrize("symbol", ["AAPL", "NVDA", "US500", "XAUJPY", "BTCJPY"])
def test_missing_contract_exports_do_not_turn_stocks_and_indices_into_forex(tmp_path, symbol):
    with pytest.raises(ValueError, match=r"spec.*tradebot fetch"):
        load_spec(tmp_path, symbol)


def test_crypto_presets_keep_the_full_asset_code():
    assert preset_spec("LINKUSDm").currency_base == "LINK"


def test_record_cache_includes_risk_and_filter_settings():
    calls = []
    tracks = TrackRecords(printing(calls=calls))
    config = BotConfig()
    tracks.get(config, "data", {})
    changed = replace(config, risk=replace(config.risk, sl_atr=2.5))
    assert tracks.cached(changed) is None
    tracks.get(changed, "data", {})
    tracks.get(config.with_strategy(adx_min=30), "data", {})
    assert len(calls) == 3


def test_record_cache_includes_history_location_and_broker_names(tmp_path):
    calls = []
    tracks = TrackRecords(printing(calls=calls))
    config = BotConfig()
    tracks.get(config, tmp_path / "first", {"EURUSDm": "EURUSD"})
    tracks.get(config, tmp_path / "second", {"EURUSDm": "EURUSD"})
    tracks.get(config, tmp_path / "second", {"EURUSD": "EURUSD"})
    assert len(calls) == 3


def test_record_cache_is_invalidated_when_history_is_replaced(tmp_path):
    calls = []
    tracks = TrackRecords(printing(calls=calls))
    config = BotConfig()
    bars = tmp_path / "EURUSD_M15.csv"
    bars.write_text("first history")
    tracks.get(config, tmp_path, {})
    bars.write_text("replacement history with additional bars")
    tracks.get(config, tmp_path, {})
    assert len(calls) == 2


def test_connect_waits_for_the_trade_server_not_only_the_terminal(fake_mt5):
    online = [False]
    fake_mt5.terminal_info = lambda: SimpleNamespace(connected=online[0], trade_allowed=True)
    broker = MT5Broker(MT5Login(), module=fake_mt5)
    waited = []

    def wait(seconds):
        waited.append(seconds)
        online[0] = True

    broker._sleep = wait
    broker.connect(attempts=2, wait_seconds=0.01)
    assert waited == [0.01]


def test_connect_refuses_a_terminal_that_never_reaches_the_trade_server(fake_mt5):
    fake_mt5.terminal_info = lambda: SimpleNamespace(connected=False, trade_allowed=True)
    with pytest.raises(MT5Error, match="trade server"):
        MT5Broker(MT5Login(), module=fake_mt5).connect(attempts=1)


def test_legacy_stops_are_preserved_without_assigning_them_to_an_unknown_account(tmp_path):
    path = tmp_path / "book.sqlite"
    book = Book(path)
    book.remember_stop(41, 1.09)
    assert book.seen_stop(41, account=(101, "Broker-A")) is None
    book.remember_stop(41, 1.08, account=(101, "Broker-A"))
    book.close()
    reopened = Book(path)
    try:
        assert reopened.seen_stop(41) == 1.09
        assert reopened.seen_stop(41, account=(101, "Broker-A")) == 1.08
        assert reopened.seen_stop(41, account=(202, "Broker-A")) is None
    finally:
        reopened.close()
