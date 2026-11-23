"""Regression cases found while reviewing configuration, broker failures and risk guards."""

import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL, advice_for, features
from test_engine import flat, setup, step

from tradebot.broker.base import OrderRequest, Position
from tradebot.broker.mt5 import MT5Broker, MT5Error
from tradebot.config import BotConfig, ConfigError, MT5Login, RiskParams, ScheduleParams, config_from_dict
from tradebot.data import load_bars
from tradebot.engine import Engine, Rejection
from tradebot.live import trading_gate
from tradebot.news import NewsCalendar
from tradebot.risk import open_risk, position_size
from tradebot.ui.monitor import clean


@pytest.mark.parametrize("key", ["dry_run", "live_trading_confirm", "allow_real_account"])
def test_live_permissions_require_actual_booleans(key):
    with pytest.raises(ConfigError, match=key):
        config_from_dict({key: "false"}, env={})


def test_gate_denies_truthy_permission_strings_even_without_validation(eurusd, config):
    _, sim, _ = setup(config, eurusd)
    unsafe = replace(config, dry_run=False, live_trading_confirm="false")
    assert not trading_gate(unsafe, sim.account(), True, True).execute


@pytest.mark.parametrize(
    "raw", [[], {"symbols": "EURUSD"}, {"risk": None}, {"overrides": []}, {"overrides": {"EURUSD": []}}]
)
def test_malformed_configuration_structure_is_a_config_error(raw):
    with pytest.raises(ConfigError):
        config_from_dict(raw, env={})


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("risk", "max_total_risk_percent", float("nan")),
        ("risk", "sl_atr", float("inf")),
        ("risk", "max_margin_percent", -1),
        ("risk", "fixed_lot", -1),
        ("management", "partial_tp", "false"),
        ("strategy", "atr_period", 0),
        ("strategy", "slow_ema", 3.5),
        ("schedule", "max_bar_age_bars", 0),
        ("backtest", "initial_balance", 0),
    ],
)
def test_invalid_numeric_and_boolean_settings_are_rejected(section, key, value):
    with pytest.raises(ConfigError, match=key):
        config_from_dict({section: {key: value}}, env={})


@pytest.mark.parametrize("volume, expected", [(0.25, 0.25), (0.76, 0.75), (1.1, 1.0)])
def test_volume_uses_the_exact_broker_step(eurusd, volume, expected):
    spec = replace(eurusd, volume_step=0.25, volume_min=0.25, volume_max=1.1)
    assert spec.normalize_volume(volume) == expected
    assert spec.normalize_volume(volume) <= volume


def test_prices_align_to_tick_size_not_just_display_digits(eurusd):
    spec = replace(eurusd, digits=2, point=0.01, tick_size=0.25)
    assert spec.round_price(100.13) == 100.25


@pytest.mark.parametrize("equity", [0.0, -100.0, float("nan"), float("inf")])
def test_invalid_equity_never_produces_a_position(eurusd, equity):
    assert not position_size(eurusd, equity, 0.001, RiskParams()).ok
    assert not position_size(eurusd, equity, 0.001, RiskParams(fixed_lot=0.1)).ok


def test_missing_stop_or_symbol_cannot_count_as_zero_open_risk(eurusd):
    position = Position(1, "EURUSD", 1, 0.1, 1.1, 0, 0, NOW)
    assert np.isinf(open_risk([position], {"EURUSD": eurusd}))
    position.sl = 1.09
    assert np.isinf(open_risk([position], {}))


def test_unknown_portfolio_risk_blocks_entry_and_serializes_for_the_dashboard(config, eurusd, monkeypatch):
    config, sim, engine = setup(config, eurusd)
    sim.start_bar("EURUSD", flat(0), NOW)
    held = Position(1, "EURUSD", 1, 0.1, 1.1, 0, 0, NOW)
    monkeypatch.setattr(sim, "positions", lambda _: [held])
    status = engine.risk_status(NOW)
    assert status.open_risk_percent is None and "cannot be measured" in status.blocked
    json.dumps(clean(status), allow_nan=False)
    plan = engine.plan_entry("EURUSD", eurusd, config.for_symbol("EURUSD"), flat(0), 0.0001, 1)
    assert isinstance(plan, Rejection) and plan.key == "portfolio risk"
    assert "no stop loss (EURUSD)" in plan.message  # not "open risk would exceed 1%"


@pytest.mark.parametrize("margin", [None, float("nan"), -1.0])
def test_missing_or_invalid_margin_estimate_blocks_entry(config, eurusd, monkeypatch, margin):
    config, sim, engine = setup(config, eurusd)
    sim.start_bar("EURUSD", flat(0), NOW)
    monkeypatch.setattr(sim, "margin_required", lambda *args: margin)
    plan = engine.plan_entry("EURUSD", eurusd, config.for_symbol("EURUSD"), flat(0), 0.0001, 1)
    assert isinstance(plan, Rejection) and plan.key == "margin"


def broker_for(fake):
    return MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)


@pytest.mark.parametrize("method", ["positions_get", "history_deals_get", "history_orders_get"])
def test_mt5_query_failure_is_not_an_empty_account(method, monkeypatch):
    fake = FakeMT5(now=NOW)
    monkeypatch.setattr(fake, method, lambda *args, **kwargs: None)
    broker = broker_for(fake)
    with pytest.raises(MT5Error):
        if method == "positions_get":
            broker.positions(None)
        elif method == "history_deals_get":
            broker.deals(NOW - timedelta(days=1), None)
        else:
            broker.opening(123)


def test_empty_opening_history_is_retried_when_the_terminal_catches_up():
    fake = FakeMT5(now=NOW)
    broker = broker_for(fake)
    assert broker.opening(123) == (None, None)
    fake.orders.append(SimpleNamespace(position_id=123, time_setup=1, volume_initial=0.2, sl=1.09))
    assert broker.opening(123) == (0.2, 1.09)


@pytest.mark.parametrize("change", [{"bid": float("nan")}, {"ask": 1.0}, {"time": 0}])
def test_adapter_refuses_bad_quotes_at_the_send_boundary(change, monkeypatch):
    fake = FakeMT5(now=NOW)
    quote = fake.symbol_info_tick(SYMBOL)
    for key, value in change.items():
        setattr(quote, key, value)
    monkeypatch.setattr(fake, "symbol_info_tick", lambda _: quote)
    broker = broker_for(fake)
    request = OrderRequest(SYMBOL, 1, 0.1, 0.0015, 0.003)
    assert not broker.open(request, 1).ok
    held = Position(1, SYMBOL, 1, 0.1, 1.1, 1.09, 1.12, NOW)
    assert not broker.close(held, None, 1, "test").ok
    assert not fake.sent


@pytest.mark.parametrize("age", [timedelta(minutes=3), timedelta(seconds=-61)])
def test_stale_or_future_quotes_cannot_produce_actionable_advice(age, monkeypatch):
    fake = FakeMT5(now=NOW)
    broker = broker_for(fake)
    original = broker.tick(SYMBOL)
    monkeypatch.setattr(broker, "tick", lambda _: replace(original, time=NOW - age))
    cfg = BotConfig(symbols=(SYMBOL,), schedule=ScheduleParams(session="all", news_file=""))
    advice = advice_for(cfg, broker, features({"signal": 1, "raw_signal": 1}))
    assert not advice.actionable
    assert "quote" in advice.detail.lower()


def test_stale_quote_prevents_live_orders_and_recovers_on_the_same_bar(monkeypatch):
    fake = FakeMT5(now=NOW)
    broker = broker_for(fake)
    tick = broker.tick(SYMBOL)
    monkeypatch.setattr(broker, "tick", lambda _: replace(tick, time=NOW - timedelta(minutes=3)))
    cfg = BotConfig(symbols=(SYMBOL,), schedule=ScheduleParams(session="all", news_file=""))
    engine = Engine(cfg, broker, live=True, execute=True)
    row = features({"signal": 1, "raw_signal": 1}).iloc[-1]
    spec = broker.symbol(SYMBOL)
    engine.on_bar(SYMBOL, spec, cfg.for_symbol(SYMBOL), row, tick.spread, NOW)
    assert not fake.sent and SYMBOL not in engine._acted_bar
    monkeypatch.setattr(broker, "tick", lambda _: tick)
    engine.on_bar(SYMBOL, spec, cfg.for_symbol(SYMBOL), row, tick.spread, NOW)
    assert len(fake.sent) == 1


def test_repolling_the_entry_bar_cannot_close_a_newer_position(config, eurusd):
    config, sim, engine = setup(config, eurusd)
    step(engine, sim, config, eurusd, flat(0, signal=1, raw_signal=1))
    step(engine, sim, config, eurusd, flat(0, signal=-1, raw_signal=-1))
    assert len(sim.positions(config.magic)) == 1 and not sim.trades


@pytest.mark.parametrize("atr", [0.0, float("nan"), float("inf")])
def test_bad_atr_never_produces_a_plan(config, eurusd, atr):
    config, sim, engine = setup(config, eurusd)
    sim.start_bar("EURUSD", flat(0), NOW)
    plan = engine.plan_entry("EURUSD", eurusd, config.for_symbol("EURUSD"), flat(0, atr=atr), 0.0001, 1)
    assert isinstance(plan, Rejection) and plan.key == "data"


def test_bad_calendar_update_keeps_known_blackouts(tmp_path):
    path = tmp_path / "news.json"
    event = {"time_utc": NOW.isoformat(), "currency": "USD", "impact": "high", "event": "CPI"}
    path.write_text(json.dumps([event]))
    calendar = NewsCalendar(path)
    assert calendar.blackout(NOW, {"USD"}, 30, 30)
    path.write_text("{unfinished json")
    assert calendar.blackout(NOW, {"USD"}, 30, 30)
    path.write_text('[{"unexpected": "format"}]')
    assert calendar.blackout(NOW, {"USD"}, 30, 30)
    path.unlink()
    assert calendar.blackout(NOW, {"USD"}, 30, 30)
    path.write_text("[]")
    assert calendar.blackout(NOW, {"USD"}, 30, 30) is None


@pytest.mark.parametrize("column,value", [("close", "NaN"), ("high", "inf"), ("time", ""), ("spread", "-1")])
def test_csv_loader_rejects_unusable_market_data(tmp_path, column, value):
    row = dict(time="2026-03-04T12:00:00Z", open="1.1", high="1.2", low="1.0", close="1.1", spread="10")
    row[column] = value
    path = tmp_path / "bars.csv"
    pd.DataFrame([row]).to_csv(path, index=False)
    with pytest.raises(ValueError):
        load_bars(path)
