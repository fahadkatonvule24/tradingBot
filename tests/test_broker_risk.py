import pytest
from conftest import at, bar, feed, make_sim

from tradebot.broker.base import BUY, SELL, OrderRequest, Position
from tradebot.config import RiskParams
from tradebot.data import preset_spec
from tradebot.risk import open_risk, position_size, tighter_stop

# --- sizing -------------------------------------------------------------------------------------


def test_position_size_risks_the_configured_percent(eurusd):
    size = position_size(eurusd, 10_000, 0.0010, RiskParams(risk_percent=0.25))
    assert size.volume == 0.25  # $25 risk / ($100 per lot for 10 pips)
    assert size.risk_amount == pytest.approx(25.0)


def test_below_minimum_lot_is_skipped_not_rounded_up(eurusd):
    size = position_size(eurusd, 200, 0.0050, RiskParams(risk_percent=0.25))
    assert not size.ok and "minimum lot" in size.reason
    forced = position_size(eurusd, 200, 0.0050, RiskParams(risk_percent=0.25, allow_min_lot_overrisk=True))
    assert forced.volume == 0.01


def test_volume_rounds_down_to_step(eurusd):
    assert eurusd.normalize_volume(0.129) == 0.12
    assert eurusd.normalize_volume(500) == 100


def test_tighter_stop_never_loosens():
    assert tighter_stop(BUY, 1.10, 1.09) == 1.10
    assert tighter_stop(BUY, 1.10, 1.11) == 1.11
    assert tighter_stop(SELL, 1.10, 1.11) == 1.10
    assert tighter_stop(SELL, 0.0, 1.11) == 1.11


def test_open_risk_drops_to_zero_once_stop_is_past_entry(eurusd):
    p = Position(1, "EURUSD", BUY, 1.0, 1.1000, 1.0990, 0, at(0))
    assert open_risk([p], {"EURUSD": eurusd}) == pytest.approx(100.0)
    p.sl = 1.1001
    assert open_risk([p], {"EURUSD": eurusd}) == 0.0


# --- simulated execution --------------------------------------------------------------------------


def opened(sim, side=BUY, sl=0.0010, tp=0.0020, volume=1.0):
    return sim.open(OrderRequest("EURUSD", side, volume, sl, tp), magic=1)


def test_buy_fills_at_ask_and_stop_fills_at_stop(eurusd):
    sim = make_sim({"EURUSD": eurusd}, min_spread=10)
    feed(sim, "EURUSD", bar(at(0), 1.1, 1.1005, 1.0995, 1.1000))
    result = opened(sim)
    assert result.price == pytest.approx(1.1001)  # bid close + 1 pip spread
    feed(sim, "EURUSD", bar(at(15), 1.1000, 1.1002, 1.0985, 1.0990))  # low pierces the stop
    (trade,) = sim.trades
    assert trade.exit_reason == "stop" and trade.exit_price == pytest.approx(1.0991)
    assert trade.pnl == pytest.approx(-100.0)  # exactly 1R on 1 lot, no commission
    assert trade.r_multiple == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open(eurusd):
    sim = make_sim({"EURUSD": eurusd})
    feed(sim, "EURUSD", bar(at(0), 1.1, 1.1005, 1.0995, 1.1000))
    opened(sim)
    feed(sim, "EURUSD", bar(at(15), 1.0970, 1.0980, 1.0960, 1.0975))
    assert sim.trades[0].exit_reason == "stop (gap)"
    assert sim.trades[0].exit_price == pytest.approx(1.0970)


def test_bar_touching_stop_and_target_counts_as_stop(eurusd):
    sim = make_sim({"EURUSD": eurusd})
    feed(sim, "EURUSD", bar(at(0), 1.1, 1.1005, 1.0995, 1.1000))
    opened(sim)
    feed(sim, "EURUSD", bar(at(15), 1.1001, 1.1030, 1.0980, 1.1000))
    assert sim.trades[0].exit_reason == "stop"


def test_sell_exits_at_ask(eurusd):
    sim = make_sim({"EURUSD": eurusd}, min_spread=10)
    feed(sim, "EURUSD", bar(at(0), 1.1, 1.1005, 1.0995, 1.1000))
    result = opened(sim, side=SELL)
    assert result.price == pytest.approx(1.1000)  # sells fill at the bid
    # target is 1.0980; the ask (bid + 1 pip) must reach it, so a bid low of 1.0980 is not enough
    feed(sim, "EURUSD", bar(at(15), 1.0995, 1.0996, 1.0980, 1.0990))
    assert not sim.trades
    feed(sim, "EURUSD", bar(at(30), 1.0990, 1.0991, 1.0978, 1.0985))
    assert sim.trades[0].exit_reason == "target" and sim.trades[0].pnl == pytest.approx(200.0)


def test_commission_is_per_lot_traded(eurusd):
    sim = make_sim({"EURUSD": eurusd}, commission=7.0)
    feed(sim, "EURUSD", bar(at(0), 1.1, 1.1005, 1.0995, 1.1000))
    opened(sim, volume=2.0)
    assert sim.balance == pytest.approx(10_000 - 7.0)  # half of $7 x 2 lots on entry
    sim.close(sim.positions(1)[0], None, 1, "test")
    assert sim.trades[0].pnl == pytest.approx(
        -2.0 * 0.0001 / 0.00001 * 1.0 - 14.0
    )  # spread + full commission


def test_partial_close_bookkeeping_and_equity(eurusd):
    sim = make_sim({"EURUSD": eurusd})
    feed(sim, "EURUSD", bar(at(0), 1.1, 1.1005, 1.0995, 1.1000))
    opened(sim, volume=1.0, tp=0.0050)
    feed(sim, "EURUSD", bar(at(15), 1.1000, 1.1022, 1.0999, 1.1021))
    assert sim.account().equity == pytest.approx(10_000 + 200.0)
    position = sim.positions(1)[0]
    sim.close(position, 0.5, 1, "partial")
    assert position.partially_closed and position.volume == 0.5
    sim.close(position, None, 1, "rest")
    assert len(sim.trades) == 1 and sim.trades[0].pnl == pytest.approx(200.0)
    assert sim.balance == pytest.approx(10_200.0)


def test_usdjpy_pip_value_follows_price():
    spec = preset_spec("USDJPY")
    sim = make_sim({"USDJPY": spec}, min_spread=0)
    feed(sim, "USDJPY", bar(at(0), 150, 150.1, 149.9, 150.0))
    sim.open(OrderRequest("USDJPY", BUY, 1.0, 0.5, 0.0), magic=1)
    feed(sim, "USDJPY", bar(at(15), 150.0, 150.0, 149.4, 149.6))
    assert sim.trades[0].pnl == pytest.approx(-0.5 * 100_000 / 149.5, rel=1e-3)
