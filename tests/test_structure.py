"""Market structure, zones, round numbers and chart patterns (tradebot.structure)."""

from __future__ import annotations

import itertools
import json

import numpy as np
import pandas as pd
import pytest

from tradebot.structure import describe, patterns, round_levels, structure_state, swings, zones

ATR = 0.0010


def path(*turns: float, leg: int = 6) -> pd.DataFrame:
    """Bars whose closes walk in straight lines between ``turns``; each bar 0.4 pip either side."""
    closes = [turns[0]]
    for a, b in itertools.pairwise(turns):
        closes += list(np.linspace(a, b, leg + 1)[1:])
    c = np.array(closes)
    time = pd.date_range("2026-03-02", periods=len(c), freq="h")
    return pd.DataFrame({"time": time, "open": np.r_[c[0], c[:-1]], "high": c + 0.00004, "low": c - 0.00004,
                         "close": c, "atr": ATR})  # fmt: skip


def test_swings_alternate_and_are_labelled():
    df = path(1.1000, 1.1030, 1.1010, 1.1050, 1.1030, 1.1070, 1.1050)
    found = swings(df)
    assert [s.kind for s in found] == ["high", "low", "high", "low", "high"]
    assert [s.label for s in found] == ["", "", "HH", "HL", "HH"]


def test_a_swing_is_only_known_three_bars_later():
    df = path(1.1000, 1.1030, 1.1010, 1.1050, 1.1030, 1.1070, 1.1050, 1.1080)
    for i in range(10, len(df)):
        assert swings(df, upto=i) == swings(
            df.iloc[: i + 1].reset_index(drop=True)
        )  # nothing from later bars
        assert all(s.i <= i - 3 for s in swings(df, upto=i))


def test_structure_up_down_range_and_its_break():
    up = swings(path(1.1000, 1.1030, 1.1010, 1.1050, 1.1030, 1.1070, 1.1050))
    assert structure_state(up, 1.1060)["state"] == "up" and structure_state(up, 1.1060)["broken"] == 0
    assert structure_state(up, 1.1020)["broken"] == -1  # closed below the last higher low, 1.1030
    down = swings(path(1.1070, 1.1040, 1.1060, 1.1020, 1.1040, 1.1000, 1.1020))
    assert structure_state(down, 1.1010)["state"] == "down"
    assert structure_state(down, 1.1045)["broken"] == 1
    flat = swings(path(1.1000, 1.1030, 1.1000, 1.1031, 1.1001, 1.1030, 1.1000))
    assert structure_state(flat, 1.1015)["state"] == "range"


def test_zones_cluster_touches_and_mark_role_reversal():
    df = path(1.1000, 1.1030, 1.1010, 1.1032, 1.1012, 1.1060, 1.1031, 1.1060)
    found = zones(swings(df), ATR, 1.1050)
    nearest = found[0]
    assert nearest["side"] == "support" and nearest["touches"] == 3  # highs 1.1030, 1.1032 and the low 1.1031
    assert nearest["flipped"] is True  # old resistance held as support
    assert nearest["low"] == pytest.approx(1.10304) and nearest["high"] == pytest.approx(1.10324)
    assert zones(swings(df), float("nan"), 1.1050) == []


def test_round_numbers_either_side():
    assert round_levels(1.1012) == {"below": 1.1, "above": 1.15, "step": pytest.approx(0.05)}
    assert (round_levels(152.3)["below"], round_levels(152.3)["above"]) == (150, 155)
    assert (round_levels(4023.0)["below"], round_levels(4023.0)["above"]) == (4000, 4050)
    assert round_levels(float("nan")) == {}


def _kinds(df: pd.DataFrame) -> dict[str, dict]:
    return {p["kind"]: p for p in patterns(df, swings(df))}


def test_double_top_forms_then_confirms_with_a_measured_target():
    forming = path(1.1000, 1.1050, 1.1020, 1.1052, 1.1030)
    top = _kinds(forming)["double_top"]
    assert (top["status"], top["side"], top["neckline"]) == ("forming", -1, pytest.approx(1.10196))
    broken = path(1.1000, 1.1050, 1.1020, 1.1052, 1.1010)
    top = _kinds(broken)["double_top"]
    assert top["status"] == "confirmed" and top["target"] == pytest.approx(1.10196 - (1.10524 - 1.10196))


def test_a_double_top_whose_highs_are_taken_out_is_dropped():
    assert "double_top" not in _kinds(path(1.1000, 1.1050, 1.1020, 1.1052, 1.1045, 1.1060, 1.1055))


def test_head_and_shoulders_and_its_mirror():
    top = _kinds(path(1.1000, 1.1040, 1.1020, 1.1070, 1.1021, 1.1041, 1.1005))
    assert top["head_shoulders"]["status"] == "confirmed" and top["head_shoulders"]["side"] == -1
    bottom = _kinds(path(1.1100, 1.1060, 1.1080, 1.1030, 1.1079, 1.1059, 1.1095))
    assert bottom["inverse_head_shoulders"]["status"] == "confirmed"
    assert bottom["inverse_head_shoulders"]["side"] == 1


def test_triangles():
    rising = _kinds(path(1.1000, 1.1050, 1.1010, 1.1050, 1.1030, 1.1049))
    assert rising["ascending_triangle"]["side"] == 1 and rising["ascending_triangle"]["status"] == "forming"
    sym = _kinds(path(1.1000, 1.1060, 1.1010, 1.1050, 1.1025, 1.1035))
    assert sym["symmetrical_triangle"]["side"] == 0 and sym["symmetrical_triangle"]["target"] is None
    broke = _kinds(path(1.1000, 1.1060, 1.1010, 1.1045, 1.1025, 1.1035))  # closed above the falling line
    assert (
        broke["symmetrical_triangle"]["side"] == 1 and broke["symmetrical_triangle"]["status"] == "confirmed"
    )


def test_describe_is_json_safe_and_names_the_structure():
    out = describe(path(1.1000, 1.1030, 1.1010, 1.1050, 1.1030, 1.1070, 1.1050), digits=5)
    assert out["state"] == "up" and out["text"].startswith("Uptrend: higher highs and higher lows")
    json.dumps(out, allow_nan=False)
    assert describe(path(1.1, 1.1001, leg=2), digits=5)["state"] == "unknown"
