"""Liquidity sweeps, fair value gaps and supply/demand zones (tradebot.smc)."""

from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest
from test_advisor_ui import advice_for, broker, cfg, fake, features  # noqa: F401 (fixtures)

from tradebot import smc


def bars(rows: list[tuple[float, float, float, float]], atr: float = 1.0) -> pd.DataFrame:
    """(open, high, low, close) rows, one hour apart, with a flat ATR."""
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame["time"] = pd.date_range("2026-03-02", periods=len(frame), freq="h")
    frame["atr"] = atr
    return frame


def quiet(n: int, price: float = 100.0) -> list[tuple[float, float, float, float]]:
    """Small alternating bars around ``price``, no pivots worth anything."""
    return [(price, price + 0.2, price - 0.2, price + (0.05 if i % 2 else -0.05)) for i in range(n)]


def with_pivot_high(level: float = 102.0) -> list[tuple[float, float, float, float]]:
    """Quiet bars with one bar reaching ``level``: a pivot high once three bars follow it."""
    rows = quiet(10)
    rows[5] = (100.0, level, 99.8, 100.1)
    return rows


# --- sweeps -----------------------------------------------------------------------------------------


def test_a_wick_above_a_swing_high_that_closes_back_below_is_a_bearish_sweep():
    rows = [*with_pivot_high(), (100.0, 102.6, 99.9, 101.4)]  # through 102, closed back under it
    out = smc.sweeps(bars(rows))
    assert (out["sweep_side"].iat[-1], out["sweep_level"].iat[-1], out["sweep_extreme"].iat[-1]) == (
        -1,
        102.0,
        102.6,
    )
    assert (out["sweep_side"].iloc[:-1] == 0).all()


def test_a_break_reclaimed_on_the_next_bar_is_a_two_bar_sweep():
    """Gold on 18 Nov 2026: one bar closed under the swing low, the next closed back above it."""
    rows = [*with_pivot_high(), (100.0, 102.6, 99.9, 102.3), (102.3, 102.9, 101.0, 101.2)]
    out = smc.sweeps(bars(rows))
    assert out["sweep_side"].iat[-2] == 0  # closed above: a break so far
    assert (out["sweep_side"].iat[-1], out["sweep_level"].iat[-1], out["sweep_extreme"].iat[-1]) == (
        -1,
        102.0,
        102.9,
    )
    assert smc.sweeps(bars(rows), reclaim=1)["sweep_side"].iat[-1] == 0  # same bar only: no sweep


def test_a_break_that_holds_for_two_bars_is_not_a_sweep_and_its_level_is_gone():
    rows = [
        *with_pivot_high(),
        (100.0, 102.6, 99.9, 102.3),
        (102.3, 102.9, 102.1, 102.5),
        (102.5, 102.6, 101, 101.2),
    ]
    out = smc.sweeps(bars(rows))
    assert (out["sweep_side"].iloc[-3:] == 0).all()


def test_a_pivot_is_only_used_once_it_is_known():
    rows = quiet(10)
    rows[7] = (100.0, 102.0, 99.8, 100.1)  # a high, but only 2 bars follow before the poke
    rows += [(100.0, 102.4, 99.9, 101.0)]
    assert smc.sweeps(bars(rows))["sweep_side"].iat[-1] == 0


def test_lows_swept_and_reclaimed_are_a_bullish_sweep():
    rows = quiet(10)
    rows[5] = (100.0, 100.2, 98.0, 99.9)
    rows += [(100.0, 100.1, 97.5, 98.6)]
    assert smc.sweeps(bars(rows))["sweep_side"].iat[-1] == 1


# --- fair value gaps and zones --------------------------------------------------------------------------


def test_a_bullish_gap_is_found_tested_once_and_filled_by_a_close_below_it():
    rows = [
        *quiet(5),
        (100.0, 100.3, 99.9, 100.2),
        (100.2, 102.5, 100.1, 102.4),
        (102.4, 103.0, 101.0, 102.8),
    ]
    found = smc.scan(bars(rows))
    (gap,) = found.gaps
    assert (gap.side, gap.bottom, gap.top) == (1, 100.3, 101.0)
    tested = [*rows, (101.5, 102.6, 100.8, 102.0)]  # dips into it, an up bar: the first test, a buy
    assert smc.scan(bars(tested)).fvg_trigger[-1] == 1
    filled = [*tested, (102.0, 102.1, 99.5, 99.8)]  # closes below the gap: gone
    assert smc.scan(bars(filled)).gaps == []


def test_small_gaps_are_noise():
    rows = [
        *quiet(5),
        (100.0, 100.3, 99.9, 100.2),
        (100.2, 100.6, 100.1, 100.5),
        (100.5, 100.8, 100.4, 100.7),
    ]
    assert smc.scan(bars(rows)).gaps == []  # 0.1 ATR


def test_small_bars_then_a_strong_move_up_leave_a_demand_zone():
    rows = [
        *quiet(5),
        (99.5, 100.5, 99.4, 100.4),  # leg in: a rally (a big body, so not part of the base)
        (100.4, 100.7, 99.9, 100.35),  # base: small bodies
        (100.35, 100.6, 100.0, 100.4),
        (100.4, 102.2, 100.3, 102.0),  # departure: body 1.6 ATR, closes above the base
    ]
    found = smc.scan(bars(rows))
    (zone,) = [z for z in found.zones if z.side > 0]
    assert zone.kind == "rally-base-rally" and (zone.distal, zone.proximal) == (99.9, 100.4)
    back = [*rows, (102.0, 102.1, 100.3, 100.55)]  # first return to the proximal edge, closes above it
    assert smc.scan(bars(back)).zone_trigger[-1] == 1
    assert smc.scan(bars(back)).at_demand[-1]
    broken = [*back, (100.9, 101.0, 99.0, 99.5)]  # a close under the distal edge breaks it
    assert not [z for z in smc.scan(bars(broken)).zones if z.side > 0]


def test_bars_in_a_row_in_a_zone_are_one_test_not_several():
    """Regression: USDJPY showed "tested 9x" for one visit of nine bars."""
    rows = [
        *quiet(5),
        (99.5, 100.5, 99.4, 100.4),
        (100.4, 100.7, 99.9, 100.35),
        (100.35, 100.6, 100.0, 100.4),
        (100.4, 102.2, 100.3, 102.0),
    ]
    one_visit = [
        *rows,
        (102.0, 102.1, 100.3, 100.6),
        (100.6, 100.7, 100.2, 100.5),
        (100.5, 100.6, 100.1, 100.4),
    ]
    (zone,) = [z for z in smc.scan(bars(one_visit)).zones if z.side > 0]
    assert zone.touches == 1
    two_visits = [*one_visit, (100.6, 101.0, 100.5, 100.9), (100.9, 101.0, 100.3, 100.8)]  # left, came back
    (zone,) = [z for z in smc.scan(bars(two_visits)).zones if z.side > 0]
    assert zone.touches == 2


def test_nothing_uses_bars_that_had_not_closed_yet():
    rng = random.Random(5)
    price, rows = 100.0, []
    for _ in range(600):
        move = rng.gauss(0, 0.6)
        o, c = price, price + move
        rows.append((o, max(o, c) + abs(rng.gauss(0, 0.3)), min(o, c) - abs(rng.gauss(0, 0.3)), c))
        price = c
    frame = bars(rows)
    whole = smc.scan(frame)
    assert (whole.sweep_side != 0).any() and whole.gaps and (whole.zone_trigger != 0).any()
    for i in rng.sample(range(50, 600), 25):
        part = smc.scan(frame.iloc[: i + 1])
        for name in ("sweep_side", "fvg_trigger", "zone_trigger", "at_demand", "at_supply", "bull_gap_near"):
            assert getattr(part, name)[-1] == getattr(whole, name)[i], (i, name)


# --- the cautions and the advice ---------------------------------------------------------------------------


def test_a_sell_at_a_demand_zone_and_a_forming_sweep_against_it_are_both_said():
    rows = [
        *quiet(5),
        (99.5, 100.5, 99.4, 100.4), (100.4, 100.7, 99.9, 100.35), (100.35, 100.6, 100.0, 100.4),
        (100.4, 102.2, 100.3, 102.0), (102.0, 102.1, 100.3, 100.55),
    ]  # fmt: skip
    closed = bars(rows)
    (zone,) = [c for c in smc.cautions(closed, None, -1, 2, 60) if c["key"] == "zone"]
    assert "tested demand zone 99.90-100.40 (rally-base-rally)" in zone["text"]  # the last bar touched it
    assert all(
        c["key"] != "zone" for c in smc.cautions(closed, None, 1, 2, 60)
    )  # a buy has no zone against it
    preview = bars([*rows, (100.55, 100.6, 99.3, 100.5)])  # forming: under the swing low 99.4, back above
    forming = [c for c in smc.cautions(closed, preview, -1, 2, 60) if c["key"] == "sweep" and c["forming"]]
    assert forming and "back above it now" in forming[0]["text"]


def test_no_join_is_offered_at_an_opposite_zone(cfg, broker, monkeypatch):  # noqa: F811 (fixtures)
    frame = features()
    frame.loc[len(frame) - 4, ["signal", "raw_signal"]] = [1, 1]  # a buy 3 bars ago: joinable
    zone = {
        "key": "zone",
        "forming": False,
        "text": "Price is at a fresh supply zone 1.1050-1.1060 (rally-base-drop).",
    }
    monkeypatch.setattr(smc, "cautions", lambda *args, **kwargs: [zone])
    a = advice_for(cfg, broker, frame)
    assert (a.action, a.headline) == ("TREND", "Uptrend: don't join here")
    assert "supply zone 1.1050-1.1060" in a.detail and "did clearly worse" in a.detail
    forming = {"key": "sweep", "forming": True, "text": "The bar still forming went past the swing high."}
    monkeypatch.setattr(smc, "cautions", lambda *args, **kwargs: [forming])
    a = advice_for(cfg, broker, frame)  # not closed yet: a warning, the join stands
    assert (a.action, a.headline) == ("JOIN", "Uptrend: you can still join the buy (see caution)")
    assert a.cautions == [forming]
    closed = {"key": "sweep", "forming": False, "text": "A bearish sweep on the last bar."}
    gap = {"key": "gap", "forming": False, "text": "Price is at an open bearish fair value gap."}
    monkeypatch.setattr(smc, "cautions", lambda *args, **kwargs: [closed, gap])
    a = advice_for(cfg, broker, frame)
    assert (a.action, a.headline) == ("TREND", "Uptrend: don't join here")
    assert "A bearish sweep on the last bar." in a.detail and "just after a sweep against it" in a.detail
    assert "fair value gap" not in a.detail  # gaps made no consistent difference to joins
    monkeypatch.setattr(smc, "cautions", lambda *args, **kwargs: [gap])
    assert advice_for(cfg, broker, frame).action == "JOIN"


@pytest.mark.parametrize("side", [1, -1])
def test_against_reports_each_caution_for_the_right_side(side):
    n = 5
    found = smc.Scan(
        sweep_side=np.array([0, 0, 0, -side, 0], dtype=np.int8), sweep_level=np.zeros(n), sweep_extreme=np.zeros(n),
        fvg_trigger=np.zeros(n), zone_trigger=np.zeros(n),
        at_demand=np.array([False] * 4 + [side < 0]), at_supply=np.array([False] * 4 + [side > 0]),
        bull_gap_near=np.zeros(n, dtype=bool), bear_gap_near=np.zeros(n, dtype=bool),
    )  # fmt: skip
    assert smc.against(found, 4, side) == ["sweep", "zone"]
    assert smc.against(found, 4, -side) == []


def test_the_caution_names_the_zone_price_is_at_not_the_newest(monkeypatch):
    """Regression: the text named the newest opposite zone, even one the last bar had just made
    while leaving it, instead of the zone the close was actually in."""
    frame = bars(quiet(10, 100.5))
    n = len(frame)
    flat = np.zeros(n)
    found = smc.Scan(
        sweep_side=np.zeros(n, dtype=np.int8), sweep_level=flat, sweep_extreme=flat, fvg_trigger=flat,
        zone_trigger=flat, at_demand=np.array([False] * (n - 1) + [True]), at_supply=np.zeros(n, dtype=bool),
        bull_gap_near=np.zeros(n, dtype=bool), bear_gap_near=np.zeros(n, dtype=bool),
        zones=[smc.Zone(3, 1, "drop-base-rally", 100.0, 100.6, 2), smc.Zone(n - 1, 1, "rally-base-rally", 103.0, 103.4, n - 3),
               smc.Zone(n - 2, 1, "rally-base-rally", 90.0, 90.4, n - 4)],
    )  # fmt: skip
    monkeypatch.setattr(smc, "scan", lambda df: found)
    (zone,) = smc.cautions(frame, None, -1, 2, 60)
    assert "demand zone 100.00-100.60 (drop-base-rally)" in zone["text"]
