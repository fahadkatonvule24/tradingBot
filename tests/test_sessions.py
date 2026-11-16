"""Session hours follow London and New York local time, so their UTC hours move with daylight saving.

Regression for a real loss: on Monday 16 November 2026 the dashboard said "Sell GBPUSD now" at
07:00 UTC. The session table held summer hours (London 07:00 UTC), but the UK was on GMT and
London opened at 08:00 UTC. The sell came an hour before the open, and the open's spike took its
stop at 08:40 UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from conftest import FakeMT5, bar

from tradebot import market
from tradebot.advisor import advise
from tradebot.broker.mt5 import MT5Broker
from tradebot.config import BotConfig, MT5Login, ScheduleParams, session_hours
from tradebot.engine import Engine
from tradebot.plan import trading_plan

SUMMER = date(2026, 7, 7)
WINTER = date(2026, 11, 16)
US_ONLY = date(2026, 3, 10)  # New York moved its clock on 8 March, London on 29 March
UK_ONLY = date(2026, 10, 28)  # London moved back on 25 October, New York on 1 November


@pytest.mark.parametrize(
    ("name", "day", "hours"),
    [
        ("london", SUMMER, (7, 16)),
        ("london", WINTER, (8, 17)),
        ("new_york", SUMMER, (12, 21)),
        ("new_york", WINTER, (13, 22)),
        ("london_new_york", SUMMER, (7, 21)),
        ("london_new_york", WINTER, (8, 22)),
        ("overlap", SUMMER, (12, 16)),
        ("overlap", WINTER, (13, 17)),
        ("london_new_york", US_ONLY, (8, 21)),
        ("overlap", US_ONLY, (12, 17)),
        ("london_new_york", UK_ONLY, (8, 21)),
        ("overlap", UK_ONLY, (12, 17)),
        ("asia", WINTER, (23, 8)),
        ("all", WINTER, (0, 24)),
    ],
)
def test_session_hours_on_the_city_clocks(name, day, hours):
    assert session_hours(name, day) == hours


def test_the_time_given_may_be_naive_aware_or_a_timestamp():
    naive = datetime(2026, 11, 16, 7)
    assert session_hours("london", naive) == (8, 17)
    assert session_hours("london", naive.replace(tzinfo=timezone.utc)) == (8, 17)
    assert session_hours("london", pd.Timestamp("2026-11-16 07:00", tz="UTC")) == (8, 17)
    # 23:30 on 15 November in New York is already 16 November in UTC
    eastern = datetime(2026, 11, 15, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert session_hours("london", eastern) == (8, 17)


def test_a_custom_window_stays_in_utc():
    s = ScheduleParams(session="custom", start_hour_utc=6, end_hour_utc=20)
    assert s.hours(datetime(2026, 7, 7)) == s.hours(datetime(2026, 11, 16)) == (6, 20)


def test_the_hour_before_london_opens_in_winter_is_outside_the_session():
    s = ScheduleParams(session="london_new_york")
    assert s.closed(datetime(2026, 11, 16, 7)) == "07:00 UTC outside 08-22"
    assert s.closed(datetime(2026, 11, 16, 8)) is None
    assert s.closed(datetime(2026, 11, 16, 21)) is None  # New York trades until 22:00 UTC in winter
    assert s.closed(datetime(2026, 7, 7, 7)) is None
    assert s.closed(datetime(2026, 7, 7, 21)) == "21:00 UTC outside 07-21"


def test_market_panel_sessions_move_with_the_clocks():
    winter = {s["name"]: s for s in market.sessions(datetime(2026, 11, 16, 7, 30))}
    assert winter["London"] == {"name": "London", "open": False, "hours": "08-17 UTC"}
    assert winter["New York"]["hours"] == "13-22 UTC"
    summer = {s["name"]: s for s in market.sessions(datetime(2026, 7, 7, 7, 30))}
    assert summer["London"] == {"name": "London", "open": True, "hours": "07-16 UTC"}


def test_the_written_plan_names_the_local_hours_and_todays_utc_hours():
    def hours(now: datetime) -> str:
        section = next(s for s in trading_plan(BotConfig(), now=now) if s.title == "What is traded, and when")
        return next(line for line in section.lines if line.startswith("Hours:"))

    assert hours(datetime(2026, 11, 16)) == (
        "Hours: London 08:00 to New York 17:00, local time (08:00 to 22:00 UTC today; the UTC hours "
        "move with daylight saving), Monday to Friday."
    )
    assert "(07:00 to 21:00 UTC today" in hours(datetime(2026, 7, 7))


# --- the advice a person sees ---------------------------------------------------------------------

SYMBOL = "EURUSDm"


def _sell_at(now: datetime) -> tuple[str, str]:
    """The advice for a sell trigger on the H1 bar that closed at ``now``'s hour."""
    fake = FakeMT5(now=now)
    broker = MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)
    cfg = BotConfig(
        symbols=(SYMBOL,), timeframe="H1", schedule=ScheduleParams(session="london_new_york", news_file="")
    )
    closed = now.replace(minute=0)
    rows = [
        bar(closed - timedelta(hours=30 - i), 1.1, 1.1004, 1.0996, 1.1, ema_fast=1.0990, ema_slow=1.0995,
            macd_hist=-0.0002, trend=-1, resistance=1.1050, support=1.0900)
        for i in range(30)
    ]  # fmt: skip
    frame = pd.DataFrame(rows)
    frame.loc[frame.index[-1], ["signal", "raw_signal"]] = [-1, -1]
    engine = Engine(cfg, broker, execute=False, live=True)
    spec = broker.symbol(SYMBOL)
    a = advise(engine, SYMBOL, spec, cfg.for_symbol(SYMBOL), frame, broker.tick(SYMBOL), now)
    return a.action, a.detail


def test_a_sell_an_hour_before_the_winter_london_open_is_a_skip():
    action, detail = _sell_at(datetime(2026, 11, 16, 7, 2))
    assert action == "SKIP" and "07:00 UTC outside 08-22" in detail


def test_the_same_sell_at_the_winter_open_or_at_07_00_in_summer_is_advice():
    assert _sell_at(datetime(2026, 11, 16, 8, 2))[0] == "SELL"
    assert _sell_at(datetime(2026, 7, 6, 7, 2))[0] == "SELL"
