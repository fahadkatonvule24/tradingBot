"""Quality decisions stay visible in advice and the generated trading plan."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL, advice_for, features

from tradebot.advisor import filter_checks
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import STRATEGIES, BotConfig, MT5Login, ScheduleParams, StrategyParams
from tradebot.data import synthetic_bars
from tradebot.plan import trading_plan
from tradebot.signal_quality import quality_checks
from tradebot.strategy import BLOCK_LABELS, compute_signals

NAMES = {
    "quality_data": "Signal quality data",
    "quality_chop": "Directional efficiency",
    "quality_candle": "Candle confirmation",
    "quality_spike": "Price spike",
    "quality_extension": "Entry extension",
}


@pytest.mark.parametrize("name", STRATEGIES)
@pytest.mark.parametrize("side", (-1, 1))
def test_advisor_quality_checks_match_signal_evaluator(name, side):
    p = StrategyParams(name=name, use_signal_quality_filter=True)
    frame = compute_signals(synthetic_bars(320, seed=18), p, "H1")
    # Include warmup, ordinary bars and both directions: no dashboard-only decision rule.
    for end in (5, 140, 241, 320):
        prefix = frame.iloc[:end]
        displayed = {check.name: check for check in filter_checks(prefix, p, side)}
        shared = quality_checks(prefix, p, side)
        assert len(shared) == (5 if name in ("ema_cross", "pullback") else 4)
        for key, passes in shared:
            assert displayed[NAMES[key]].ok == bool(passes.iloc[-1])


def test_quality_rejection_is_visible_as_blocked_advice():
    p = StrategyParams(use_signal_quality_filter=True)
    cfg = BotConfig(
        symbols=(SYMBOL,),
        timeframe="M15",
        strategy=p,
        schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""),
    )
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=FakeMT5(now=NOW), clock=lambda: NOW)
    frame = features(
        {
            "raw_signal": 1,
            "signal": 0,
            "block": "quality_candle",
            "quality_data": True,
            "quality_efficiency": 0.8,
            "quality_body": -0.1,
            "quality_close": 0.3,
            "quality_range_atr": 1.0,
            "quality_extension_atr": 0.5,
        }
    )
    advice = advice_for(cfg, broker, frame)
    checks = {check.name: check for check in advice.checks}
    assert advice.action == "BLOCKED" and not advice.actionable
    assert BLOCK_LABELS["quality_candle"] in advice.detail
    assert not checks["Candle confirmation"].ok
    assert "-0.10" in checks["Candle confirmation"].detail
    assert "0.30" in checks["Candle confirmation"].detail
    assert "0.65" in checks["Candle confirmation"].detail


def test_quality_details_describe_sells_and_missing_values():
    p = StrategyParams(name="retest", use_signal_quality_filter=True)
    frame = features(
        {
            "quality_data": True,
            "quality_body": -0.6,
            "quality_close": 0.2,
            "quality_range_atr": 1.2,
            "quality_extension_atr": -1.1,
        }
    )
    checks = {check.name: check for check in filter_checks(frame, p, -1)}
    assert checks["Candle confirmation"].ok
    assert "0.60" in checks["Candle confirmation"].detail
    assert "0.80 toward the low" in checks["Candle confirmation"].detail
    assert "1.10" in checks["Entry extension"].detail
    missing = {check.name: check for check in filter_checks(features(), p, -1)}
    assert not missing["Signal quality data"].ok
    assert "unavailable" in missing["Price spike"].detail
    assert "nan" not in missing["Price spike"].detail


@pytest.mark.parametrize("name", STRATEGIES)
def test_plan_records_active_quality_thresholds_and_exit_behavior(name):
    p = StrategyParams(
        name=name,
        use_signal_quality_filter=True,
        quality_period=17,
        quality_min_efficiency=0.3,
        quality_min_body_ratio=0.4,
        quality_min_close_position=0.75,
        quality_max_range_atr=2.2,
        quality_max_extension_atr=1.8,
    )
    config = BotConfig(strategy=p)
    text = "\n".join(line for section in trading_plan(config, now=NOW) for line in section.lines)
    assert "40% of the candle range" in text and "75% of the way from low to high" in text
    assert "2.2 times the previous bar's ATR" in text and "1.8 previous-bar ATR" in text
    assert "Signal quality checks only block new entries" in text
    assert "do not estimate a win probability" in text
    assert ("Avoid chop:" in text) == (name in ("ema_cross", "pullback"))
    if name in ("ema_cross", "pullback"):
        assert "prior 17 bars" in text and "at least 0.3" in text
    disabled = replace(config, strategy=replace(p, use_signal_quality_filter=False))
    off = "\n".join(line for section in trading_plan(disabled, now=NOW) for line in section.lines)
    assert "Signal quality:" not in off and "Avoid spikes:" not in off
    assert not any(check.name in NAMES.values() for check in filter_checks(features(), disabled.strategy, 1))
