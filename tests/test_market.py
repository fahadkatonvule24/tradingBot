"""The market-wide view: strength, correlation, exposure, levels."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL, features

from tradebot import market
from tradebot.broker.base import Position
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams
from tradebot.data import preset_spec
from tradebot.ui.monitor import Monitor

EUR, GBP, JPY = preset_spec("EURUSD"), preset_spec("GBPUSD"), preset_spec("USDJPY")
SPECS = {"EURUSD": EUR, "GBPUSD": GBP, "USDJPY": JPY}


def frame(closes, atr: float = 0.001) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    times = pd.date_range("2026-03-02", periods=len(closes), freq="h")
    return pd.DataFrame({"time": times, "close": closes, "atr": atr})


def position(symbol: str, side: int, volume: float = 0.1, price: float = 1.1) -> Position:
    return Position(1, symbol, side, volume, price, 0.0, 0.0, datetime(2026, 3, 4))


def test_legs_of_a_pair_and_of_an_index():
    assert market.legs(EUR) == [("EUR", 1), ("USD", -1)]
    index = replace(EUR, name="US500", currency_base="USD", currency_profit="USD")
    assert market.legs(index) == [("US500", 1)]  # buying an index is not a bet against the dollar


def test_strength_averages_each_currency_over_its_pairs():
    up, down = np.linspace(1.10, 1.11, 30), np.linspace(1.30, 1.29, 30)
    scores = market.strength({"EURUSD": frame(up), "GBPUSD": frame(down)}, SPECS, bars=25)
    move = (up[-1] - up[-26]) / (0.001 * 5)  # in ATR, scaled by sqrt(25)
    assert scores["EUR"] == pytest.approx(move) and scores["GBP"] == pytest.approx(-move)
    assert scores["USD"] == pytest.approx(0.0)  # down against EUR, up against GBP
    table = market.strength_table({"EURUSD": frame(up), "GBPUSD": frame(down)}, SPECS, bar_minutes=60)
    assert [r["asset"] for r in table] == ["EUR", "USD", "GBP"] and table[1]["pairs"] == 2
    assert table[0]["week"] is None  # 30 bars are not a week of H1
    assert market.summary(table).startswith("Over the last day EUR moved most upwards")


def test_correlation_needs_enough_shared_bars():
    rng = np.random.default_rng(3)
    steps = rng.normal(0, 0.0005, 300)
    a = 1.1 + np.cumsum(steps)
    b = 1.3 + np.cumsum(steps * 0.9 + rng.normal(0, 0.0001, 300))
    c = 150 - np.cumsum(steps) * 100
    out = market.correlation({"EURUSD": frame(a), "GBPUSD": frame(b), "USDJPY": frame(c, atr=0.1)})
    assert out["symbols"] == ["EURUSD", "GBPUSD", "USDJPY"] and out["bars"] == 299
    assert out["matrix"][0][1] > 0.9 and out["matrix"][0][2] < -0.9 and out["matrix"][1][1] == 1.0
    assert market.correlation({"EURUSD": frame(a[:20]), "GBPUSD": frame(b[:20])}) is None
    held = [position("EURUSD", 1), position("GBPUSD", 1), position("USDJPY", 1)]
    warnings = market.correlated_positions(held, out)
    assert warnings[0].startswith("buy EURUSD and buy GBPUSD have moved together (correlation +0.9")
    assert "count them as one larger position" in warnings[0]
    assert "buy EURUSD and buy USDJPY have moved against each other" in warnings[1]  # short and long dollars
    hedge = market.correlated_positions([position("EURUSD", 1), position("GBPUSD", -1)], out)
    assert "against each other" in hedge[0] and "largely cancel out" in hedge[0]


def test_exposure_nets_currencies_across_positions():
    held = [
        position("EURUSD", 1, 0.1, 1.10),
        position("GBPUSD", 1, 0.1, 1.30),
        position("USDJPY", -1, 0.2, 150.0),
    ]
    rows = market.exposure(held, SPECS, {"EURUSD": 1.10, "GBPUSD": 1.30, "USDJPY": 150.0}, equity=10_000)
    by_asset = {r["asset"]: r for r in rows}
    # 0.1 lot EURUSD controls 11,000 USD, GBPUSD 13,000; the 0.2-lot USDJPY sell is 20,000 USD short dollars
    assert by_asset["EUR"]["amount"] == pytest.approx(11_000)
    assert by_asset["USD"]["amount"] == pytest.approx(-11_000 - 13_000 - 20_000, rel=5e-3)
    assert by_asset["USD"]["percent"] == pytest.approx(-440, rel=5e-3)
    assert by_asset["USD"]["through"] == ["EURUSD", "GBPUSD", "USDJPY"] and rows[0]["asset"] == "USD"


def test_overlap_names_the_bet_a_new_trade_would_add_to():
    held = [position("EURUSD", 1), position("USDJPY", -1)]
    text = market.overlap("GBPUSD", 1, SPECS, held)
    assert text.startswith(
        "A buy here adds to a bet you already hold: short USD (your buy EURUSD, sell USDJPY)."
    )
    assert market.overlap("GBPUSD", -1, SPECS, held) == ""  # a sell is long dollars: the other way
    assert market.overlap("GBPUSD", 1, SPECS, []) == ""


def test_key_levels_pivots_weeks_and_range_used():
    # two weeks of daily bars opening 22:00 UTC, as the broker's days do; the last closed one is Tuesday's
    opens = pd.date_range("2026-02-22 22:00", periods=10, freq="D")
    opens = opens[opens.day_of_week.isin([6, 0, 1, 2, 3])][:8]  # Sun..Thu evenings = Mon..Fri sessions
    daily = pd.DataFrame({
        "time": opens, "open": 1.10,
        "high": [1.105, 1.106, 1.107, 1.108, 1.109, 1.112, 1.111, 1.110],
        "low": [1.095, 1.096, 1.097, 1.098, 1.099, 1.102, 1.101, 1.100], "close": 1.104,
    })  # fmt: skip
    today = {
        "time": opens[-1] + timedelta(days=1),
        "open": 1.104,
        "high": 1.1065,
        "low": 1.1015,
        "close": 1.105,
    }
    out = market.key_levels(daily, today, price=1.105, atr=0.001)
    levels = {r["name"]: r["price"] for r in out["levels"]}
    pivot = (1.110 + 1.100 + 1.104) / 3
    assert levels["Previous day high"] == 1.110 and levels["Previous day low"] == 1.100
    assert levels["Pivot"] == pytest.approx(pivot) and levels["R1"] == pytest.approx(2 * pivot - 1.100)
    assert levels["This week's high"] == 1.112 and levels["This week's low"] == 1.100
    assert levels["Previous week high"] == 1.109 and levels["Previous week low"] == 1.095
    assert out["adr"] == pytest.approx(0.010) and out["range_used"] == pytest.approx(50.0)
    assert out["nearest"]["name"] == "Pivot" and out["nearest"]["away_atr"] == pytest.approx(-0.33, abs=0.01)
    assert [r["price"] for r in out["levels"]] == sorted(levels.values(), reverse=True)
    closed = market.key_levels(daily, None, price=1.105, atr=0.001)  # the weekend: nothing is forming
    assert closed["range_used"] is None and "Today's open" not in {r["name"] for r in closed["levels"]}
    assert market.key_levels(daily.head(1), today, 1.105, 0.001) is None


def test_volatility_ranks_the_current_atr_against_its_own_history():
    quiet = frame(np.full(200, 1.1))
    quiet["atr"] = np.linspace(0.002, 0.001, 200)
    assert market.volatility(quiet)["label"] == "quiet" and market.volatility(quiet)["percentile"] == 0
    busy = quiet.assign(atr=np.linspace(0.001, 0.002, 200))
    assert market.volatility(busy)["label"] == "busy"
    assert market.volatility(quiet.head(20)) is None


def test_sessions_follow_the_clock_and_the_weekend():
    overlap = market.sessions(datetime(2026, 3, 4, 13, 0))  # a Wednesday
    assert [(s["name"], s["open"]) for s in overlap] == [
        ("Asia", False),
        ("London", True),
        ("New York", True),
    ]
    assert [s["open"] for s in market.sessions(datetime(2026, 3, 4, 23, 30))] == [True, False, False]
    assert not any(s["open"] for s in market.sessions(datetime(2026, 3, 7, 13, 0)))  # Saturday


def test_the_dashboard_state_carries_the_market_view():
    fake = FakeMT5(now=NOW)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    cfg = BotConfig(symbols=(SYMBOL,), timeframe="M15",
                    schedule=ScheduleParams(session="all", trade_weekends=True, news_file=""))  # fmt: skip
    monitor = Monitor(cfg, broker, [SYMBOL])
    monitor._features[SYMBOL] = features()
    monitor._fetched[SYMBOL] = time.monotonic()
    monitor.refresh()
    state = monitor.snapshot()
    json.dumps(state, allow_nan=False)
    view = state["market"]
    assert state["book"] is None and view["exposure"] == [] and len(view["sessions"]) == 3
    detail = view["symbols"][SYMBOL]
    assert detail["levels"]["nearest"]["name"] and detail["rsi"] == 50.0 and detail["swap"] is None
    assert detail["overlap"] == {"buy": "", "sell": ""}
    assert monitor.levels_text(SYMBOL).startswith(f"{SYMBOL} levels, from the top:")
    # 12:07 UTC on 4 March: New York is still on winter time and opens at 13:00 UTC
    assert monitor.market_text().startswith("Sessions: Asia closed, London open, New York closed")


def test_swap_is_converted_to_money_per_lot(fake_mt5):
    info = fake_mt5.symbols["EURUSDm"]
    info.swap_mode, info.swap_long, info.swap_short, info.swap_rollover3days = 1, -7.5, 2.1, 3
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake_mt5, clock=lambda: fake_mt5.utc_now)
    assert broker.swap("EURUSDm") == {
        "long": pytest.approx(-7.5),
        "short": pytest.approx(2.1),
        "triple": "Wednesday",
    }
    info.swap_mode = 5  # charged as an interest rate: not converted, not guessed
    assert broker.swap("EURUSDm") is None


def test_the_fx_week_opens_on_sunday_evening():
    from datetime import datetime

    from tradebot.market import fx_weekend, sessions

    assert fx_weekend(datetime(2026, 11, 22, 21, 0))  # Sunday 16:00 New York: still shut
    assert not fx_weekend(datetime(2026, 11, 22, 23, 0))  # Sunday 18:00 New York: open
    asia = {s["name"]: s["open"] for s in sessions(datetime(2026, 11, 22, 23, 30))}["Asia"]
    assert asia is True  # it used to show closed all Sunday
    assert fx_weekend(datetime(2026, 11, 20, 22, 30))  # Friday 17:30 New York: closed
    assert not fx_weekend(datetime(2026, 11, 20, 21, 30))  # Friday 16:30 New York: open
    assert fx_weekend(datetime(2026, 11, 21, 12, 0))  # Saturday
