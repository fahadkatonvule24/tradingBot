"""The analysis combines evidence without making an order decision or inventing confidence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from tradebot.analysis import assess, format_text, semantic_key
from tradebot.ui.monitor import clean

NOW = datetime(2026, 3, 4, 12, 7)


def bars(n: int = 40) -> pd.DataFrame:
    close = 1.10 + np.arange(n) * 0.0001
    return pd.DataFrame(
        {
            "time": pd.date_range(end="2026-03-04 11:45", periods=n, freq="15min"),
            "close": close,
            "high": close + 0.0003,
            "low": close - 0.0003,
            "ema_fast": close - 0.0001,
            "ema_slow": close - 0.0007,
            "atr": 0.001,
            "adx": 30.0,
            "macd_hist": 0.0002,
            "rsi": 55.0,
            "trend_H1": 1,
            "trend_H4": 1,
        }
    )


def analyze(frame=None, trend=None, setups=(), context=None, **changes):
    kwargs = {
        "timeframe": "M15",
        "bar_minutes": 15,
        "now": NOW,
        "quote_time": NOW,
        "price": 1.104,
        "digits": 5,
    }
    kwargs.update(changes)
    return assess(
        bars() if frame is None else frame,
        {"state": "intact", "signs": []} if trend is None else trend,
        setups,
        context,
        **kwargs,
    )


def test_aligned_context_has_explanations_and_two_conditional_scenarios():
    result = analyze()
    assert (result["status"], result["regime"], result["bias"]) == ("ready", "aligned trend", 1)
    assert "H1 up, H4 up" in " ".join(result["evidence"])
    assert result["as_of"] == datetime(2026, 3, 4, 12)
    assert result["valid_until"] == NOW + timedelta(seconds=120)
    bullish, bearish = result["scenarios"]
    assert bullish["level"] > bearish["level"]
    assert bullish["invalidation_level"] == bearish["level"]
    assert bearish["invalidation_level"] == bullish["level"]
    assert "completed M15 bar" in bullish["confirmation"]
    assert "probability" not in result and "confidence" not in result
    assert "not a forecast" in result["limitations"]
    json.dumps(clean(result), allow_nan=False)


def test_downtrend_is_symmetric():
    frame = bars().assign(trend_H1=-1, trend_H4=-1, macd_hist=-0.0002)
    frame["ema_fast"], frame["ema_slow"] = frame["close"] + 0.0001, frame["close"] + 0.0007
    result = analyze(frame)
    assert (result["regime"], result["bias"]) == ("aligned trend", -1)
    assert "aligned down" in result["headline"]


def test_conflicting_higher_timeframes_do_not_become_a_directional_vote():
    result = analyze(bars().assign(trend_H4=-1))
    assert (result["regime"], result["bias"]) == ("mixed", 0)
    assert "opposite directions" in " ".join(result["conflicts"])
    assert len(result["scenarios"]) == 2


def test_weak_adx_is_not_claimed_to_prove_a_range():
    result = analyze(bars().assign(adx=12))
    assert (result["regime"], result["bias"]) == ("weak trend", 0)
    assert "range is not yet established" in result["headline"]


def test_opposing_momentum_is_a_possible_pullback_not_a_reversal_prediction():
    result = analyze(bars().assign(macd_hist=-0.0001))
    assert (result["regime"], result["bias"]) == ("pullback", 1)
    assert "possible pullback" in result["headline"]
    assert "continuation is unconfirmed" in " ".join(result["conflicts"])


def test_weakening_trend_preserves_conflicting_evidence():
    result = analyze(
        trend={"state": "ending", "signs": [{"key": "price", "text": "Price closed below the slow average."}]}
    )
    assert (result["regime"], result["bias"]) == ("transition", 0)
    assert "Price closed below the slow average." in result["conflicts"]
    assert "reversal remain open" in result["headline"]


def test_wait_side_does_not_count_as_a_confirmed_setup():
    waiting = [
        {"name": "ema_cross", "action": "WAIT", "side": -1},
        {"name": "breakout", "action": "WAIT", "side": -1},
    ]
    assert analyze(setups=waiting) == analyze()


def test_analysis_is_independent_of_account_execution_blockers_and_active_setup():
    setup = {"name": "breakout", "action": "BUY", "side": 1, "active": True}
    expected = analyze(setups=[setup])
    result = analyze(
        setups=[{**setup, "action": "SKIP", "active": False, "text": "Account has no margin"}],
        context={"account": {"equity": 1}, "gate": False},
    )
    assert result == expected
    other = {"name": "pullback", "action": "SELL", "side": -1}
    mixed = analyze(setups=[setup, other])
    assert mixed["bias"] == expected["bias"]  # setup counts are not directional votes
    assert "triggers point in different directions" in " ".join(mixed["conflicts"])


def test_a_trigger_against_the_picture_is_listed_as_a_conflict():
    """Regression: a pullback buy fired in a strong H1 downtrend and the card said "No directional
    conflict" (16 Nov 2026, AUDUSD)."""
    frame = bars().assign(trend_H1=-1, trend_H4=-1, macd_hist=-0.0002)
    frame["ema_fast"], frame["ema_slow"] = frame["close"] + 0.0001, frame["close"] + 0.0007
    against = analyze(
        frame, setups=[{"name": "pullback", "label": "Pullback", "action": "BLOCKED", "side": 1}]
    )
    assert (against["regime"], against["bias"]) == ("aligned trend", -1)  # the picture is unchanged
    assert "Pullback triggered up, against the down picture above." in against["conflicts"]
    along = analyze(frame, setups=[{"name": "breakout", "label": "Breakout", "action": "SELL", "side": -1}])
    assert not any("against the" in c for c in along["conflicts"])


@pytest.mark.parametrize(
    "change, status",
    [
        ({"quote_time": NOW - timedelta(seconds=121)}, "stale"),
        ({"quote_time": NOW + timedelta(seconds=61)}, "incomplete"),  # a few seconds ahead is clock drift
        ({"quote_time": None}, "incomplete"),
        ({"now": NOW + timedelta(hours=1), "quote_time": NOW + timedelta(hours=1)}, "stale"),
        ({"price": float("nan")}, "incomplete"),
        ({"price": float("inf")}, "incomplete"),
    ],
)
def test_invalid_or_stale_inputs_suppress_current_scenarios(change, status):
    result = analyze(**change)
    assert result["status"] == status and result["bias"] == 0 and result["scenarios"] == []
    json.dumps(clean(result), allow_nan=False)


def test_missing_higher_timeframe_direction_is_not_neutral_evidence():
    for frame in (
        bars().assign(trend_H4=0),
        bars().assign(trend_H4=np.nan),
        bars().drop(columns=["trend_H1", "trend_H4"]),
    ):
        result = analyze(frame)
        assert result["status"] == "incomplete" and result["scenarios"] == []
    fallback = analyze(
        bars().drop(columns=["trend_H1", "trend_H4"]), trend={"higher": [{"timeframe": "H4", "direction": 1}]}
    )
    assert fallback["status"] == "ready"


def test_incomplete_features_and_bad_bar_timestamps_are_reported():
    bad_frames = [
        bars().assign(atr=0),
        bars().assign(macd_hist=np.nan),
        bars().drop(columns=["adx"]),
        bars().assign(time=pd.NaT),
        pd.concat([bars(), bars().tail(1)], ignore_index=True),
        bars().iloc[::-1],
        pd.DataFrame(),
    ]
    for frame in bad_frames:
        result = analyze(frame)
        assert result["status"] == "incomplete" and not result["scenarios"]


def test_future_or_forming_bars_are_rejected_before_they_can_affect_a_scenario():
    frame = bars()
    forming = frame.tail(1).copy()
    forming["time"] = pd.Timestamp("2026-03-04 12:00")
    forming["high"] = 100.0
    contaminated = pd.concat([frame, forming], ignore_index=True)
    result = analyze(contaminated)
    assert result["status"] == "incomplete" and not result["scenarios"]
    assert "not closed yet" in result["headline"]
    # Only the supplied prefix is available at its assessment time; later observations cannot leak in.
    for end in (20, 30, 40):
        prefix = contaminated.iloc[:end].copy()
        at = prefix.iloc[-1]["time"].to_pydatetime() + timedelta(minutes=16)
        first = analyze(prefix, now=at, quote_time=at)
        changed = contaminated.copy()
        changed.loc[end:, ["high", "low", "close"]] = 999.0
        assert analyze(changed.iloc[:end], now=at, quote_time=at) == first


def test_forming_daily_and_weekly_extrema_do_not_become_confirmed_levels():
    close = bars().iloc[-1]["close"]
    forming = {
        "levels": {
            "levels": [
                {"name": "This week's high", "price": close + 0.00001},
                {"name": "This week's low", "price": close - 0.00001},
                {"name": "Today's open", "price": close + 0.00001},
            ]
        }
    }
    assert analyze(context=forming)["scenarios"] == analyze()["scenarios"]
    historical = {
        "levels": {
            "levels": [
                {"name": "Previous day high", "price": close + 0.0001},
                {"name": "Previous week low", "price": close - 0.0001},
            ]
        }
    }
    result = analyze(context=historical)
    assert "previous day high" in result["scenarios"][0]["confirmation"]
    assert "previous week low" in result["scenarios"][1]["confirmation"]


def test_a_level_equal_to_the_close_does_not_destroy_the_two_sided_scenario():
    context = {
        "levels": {"levels": [{"name": "Previous day close", "price": float(bars().iloc[-1]["close"])}]}
    }
    assert analyze(context=context)["scenarios"] == analyze()["scenarios"]


def test_nonfinite_historical_ranges_do_not_generate_levels():
    frame = bars()
    frame.loc[frame.index[-5], "high"] = np.inf
    result = analyze(frame)
    assert result["status"] == "ready" and result["scenarios"] == []
    json.dumps(clean(result), allow_nan=False)


def test_volatility_and_daily_range_are_context_not_directional_signals():
    plain = analyze()
    result = analyze(context={"volatility": {"percentile": 99}, "levels": {"range_used": 150}})
    assert (result["regime"], result["bias"]) == (plain["regime"], plain["bias"])
    assert "not direction" in " ".join(result["evidence"])
    assert "not a price ceiling" in " ".join(result["conflicts"])


def test_text_and_semantic_key_do_not_promote_an_unconfirmed_scenario():
    result = analyze()
    text = format_text("EURUSD", result)
    assert "EURUSD analysis:" in text and "2026-03-04 12:00 UTC" in text
    assert "Bullish scenario" in text and "Bearish scenario" in text and "Invalidation" in text
    assert "not a forecast" in text
    updated = analyze(now=NOW + timedelta(seconds=10), quote_time=NOW + timedelta(seconds=10), price=1.1043)
    assert semantic_key(result) == semantic_key(updated)
    assert semantic_key(result) != semantic_key(analyze(bars().assign(adx=12)))


def test_aware_and_naive_timestamps_describe_the_same_utc_instant():
    assert (
        analyze(now=NOW.replace(tzinfo=timezone.utc), quote_time=NOW.replace(tzinfo=timezone.utc))
        == analyze()
    )


def test_a_bar_closing_on_its_high_still_gets_both_scenarios():
    """Regression: the window's own high equal to the close counted as both boundaries, so a
    strong bar that closed on its high left the card with no scenarios at all."""
    frame = bars()
    frame.loc[frame.index[-1], "high"] = frame["close"].iloc[-1] + 0.0010  # a new 40-bar high...
    frame.loc[frame.index[-1], "close"] = frame["high"].iloc[-1]  # ...and the bar closed on it
    frame.loc[frame.index[-1], "ema_fast"] = frame["close"].iloc[-1] - 0.0001
    above = float(frame["close"].iloc[-1]) + 0.0020
    result = analyze(frame, context={"levels": {"levels": [{"name": "Previous week high", "price": above}]}},
                     price=float(frame["close"].iloc[-1]))  # fmt: skip
    assert [s["level"] for s in result["scenarios"]] == [
        round(above, 5),
        pytest.approx(result["scenarios"][1]["level"]),
    ]
    assert result["scenarios"][0]["level"] == round(above, 5)
