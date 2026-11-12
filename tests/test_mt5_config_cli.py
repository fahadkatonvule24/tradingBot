"""MT5 adapter (against a fake MetaTrader5 module), configuration, trading gate and CLI."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradebot.broker.base import BUY, Account, OrderRequest
from tradebot.broker.mt5 import MT5Broker
from tradebot.cli import main
from tradebot.config import BotConfig, ConfigError, MT5Login, config_from_dict, load_config
from tradebot.data import synthetic_bars
from tradebot.live import trading_gate

ROOT = Path(__file__).resolve().parent.parent


def broker_for(fake):
    return MT5Broker(MT5Login(), module=fake, clock=lambda: fake.utc_now)


# --- MT5 adapter --------------------------------------------------------------------------------


def test_resolves_broker_suffix(fake_mt5):
    assert broker_for(fake_mt5).resolve("EURUSD") == "EURUSDm"
    assert broker_for(fake_mt5).resolve("GBPUSD") is None


def test_tick_value_comes_from_the_profit_calculator(fake_mt5):
    """Regression: the broker's trade_tick_value field can be wrong (0.1 for gold on MetaQuotes-Demo)."""
    fake_mt5.symbols["EURUSDm"].trade_tick_value = 0.1
    assert broker_for(fake_mt5).symbol("EURUSDm").tick_value == pytest.approx(1.0)


def test_detects_server_offset(fake_mt5):
    assert broker_for(fake_mt5).offset == timedelta(hours=2)


def test_bars_are_closed_and_in_utc(fake_mt5):
    bars = broker_for(fake_mt5).bars("EURUSDm", "M15", 50)
    assert len(bars) == 50
    last_close = bars["time"].iloc[-1] + timedelta(minutes=15)
    assert last_close <= fake_mt5.utc_now  # the forming 12:00 bar is gone
    assert bars["time"].iloc[-1] == datetime(2026, 3, 4, 11, 45)


def test_positions_recover_original_volume_and_stop(fake_mt5):
    fake_mt5.positions = [SimpleNamespace(ticket=5, magic=1, symbol="EURUSDm", type=0, volume=0.05, price_open=1.1, sl=1.1002,
                                          tp=1.12, time=fake_mt5.server_epoch(datetime(2026, 3, 4, 9)), profit=3.0, comment="")]  # fmt: skip
    fake_mt5.orders = [SimpleNamespace(position_id=5, time_setup=1, volume_initial=0.10, sl=1.0985)]
    (p,) = broker_for(fake_mt5).positions(magic=1)
    assert p.partially_closed and p.initial_volume == 0.10
    assert p.initial_risk == pytest.approx(0.0015)  # from the original stop, not the moved one
    assert p.time_open == datetime(2026, 3, 4, 9)


def test_deals_are_net_and_utc(fake_mt5):
    t = fake_mt5.server_epoch(datetime(2026, 3, 4, 10))
    fake_mt5.deals = [
        SimpleNamespace(ticket=1, position_id=5, symbol="EURUSDm", time=t, type=1, volume=0.1, price=1.1, profit=-20.0,
                        commission=-0.7, swap=-0.3, fee=0.0, magic=1, entry=1, comment=""),
        SimpleNamespace(ticket=2, position_id=6, symbol="EURUSDm", time=t, type=1, volume=0.1, price=1.1, profit=50.0,
                        commission=0, swap=0, fee=0, magic=999, entry=1, comment=""),
    ]  # fmt: skip
    (deal,) = broker_for(fake_mt5).deals(datetime(2026, 3, 4), magic=1)
    assert (
        deal.profit == pytest.approx(-21.0) and deal.time == datetime(2026, 3, 4, 10) and deal.entry == "out"
    )


def test_open_sets_stops_from_fill_price_and_retries_filling(fake_mt5):
    fake_mt5.retcodes = [10030]  # first filling mode rejected
    result = broker_for(fake_mt5).open(OrderRequest("EURUSDm", BUY, 0.1, 0.0015, 0.003, "tradebot"), magic=7)
    assert result.ok and len(fake_mt5.sent) == 2
    first, second = fake_mt5.sent
    assert first["type_filling"] != second["type_filling"]
    assert second["sl"] == pytest.approx(1.10012 - 0.0015) and second["tp"] == pytest.approx(1.10012 + 0.003)
    assert second["magic"] == 7


# --- trading gate -------------------------------------------------------------------------------


def demo(real=False):
    return Account(10_000, 10_000, 0, "USD", real)


@pytest.mark.parametrize(
    ("changes", "live_flag", "account", "terminal", "execute"),
    [
        ({}, True, demo(), True, False),  # defaults are dry run
        ({"dry_run": False}, True, demo(), True, False),  # confirm flag missing
        ({"dry_run": False, "live_trading_confirm": True}, False, demo(), True, False),  # no --live
        ({"dry_run": False, "live_trading_confirm": True}, True, demo(), False, False),  # algo trading off
        ({"dry_run": False, "live_trading_confirm": True}, True, demo(real=True), True, False),  # real money
        ({"dry_run": False, "live_trading_confirm": True}, True, demo(), True, True),
        (
            {"dry_run": False, "live_trading_confirm": True, "allow_real_account": True},
            True,
            demo(real=True),
            True,
            True,
        ),
    ],
)
def test_trading_gate(changes, live_flag, account, terminal, execute):
    assert trading_gate(replace(BotConfig(), **changes), account, live_flag, terminal).execute is execute


# --- configuration ------------------------------------------------------------------------------


def test_shipped_config_is_safe_and_valid():
    config = load_config(ROOT / "config" / "config.json", env={})
    assert config.dry_run and not config.live_trading_confirm and not config.allow_real_account
    assert config.for_symbol("XAUUSD").backtest.min_spread_points == 25


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError, match="risk_precent"):
        config_from_dict({"risk": {"risk_precent": 1}}, env={})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"strategy": {"fast_ema": 60, "slow_ema": 50}}, "fast_ema"),
        ({"timeframe": "M7"}, "timeframe"),
        ({"risk": {"risk_percent": 10}}, "risk_percent"),
        ({"overrides": {"BTCUSD": {"risk": {"sl_atr": 2}}}}, "not configured"),
        ({"schedule": {"session": "tokyo"}}, "session"),
    ],
)
def test_validation(raw, message):
    with pytest.raises(ConfigError, match=message):
        config_from_dict(raw, env={})


def test_overrides_match_broker_suffix_and_env_credentials():
    raw = {"symbols": ["EURUSD"], "overrides": {"EURUSD": {"risk": {"sl_atr": 2.5}}}}
    config = config_from_dict(raw, env={"MT5_LOGIN": "123", "MT5_PASSWORD": "pw", "MT5_SERVER": "X"})
    assert config.for_symbol("EURUSDm").risk.sl_atr == 2.5
    assert config.mt5.login == 123 and config.mt5.password == "pw"
    assert "pw" not in repr(config.mt5)


# --- CLI ----------------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    synthetic_bars(n=5000, seed=5).to_csv(data / "EURUSD_M15.csv", index=False)
    raw = json.loads((ROOT / "config" / "config.json").read_text())
    raw.update(
        symbols=["EURUSD"], timeframe="M15", overrides={}, schedule={"session": "all", "news_file": ""}
    )
    (tmp_path / "config.json").write_text(json.dumps(raw))
    return tmp_path


def test_backtest_command(workspace, capsys):
    code = main(
        [
            "-c",
            str(workspace / "config.json"),
            "backtest",
            "--data",
            str(workspace / "data"),
            "-o",
            str(workspace / "out"),
        ]
    )
    assert code == 0
    html = (workspace / "out" / "backtest_ema_cross_M15.html").read_text(encoding="utf-8")
    assert "<svg" in html and "Equity" in html
    assert (workspace / "out" / "trades_ema_cross_M15.csv").exists()


def test_optimize_command(workspace, monkeypatch):
    from tradebot import optimize

    monkeypatch.setitem(optimize.DEFAULT_GRIDS, "breakout", {"strategy.breakout_period": [20, 40]})
    out = workspace / "out"
    code = main(["-c", str(workspace / "config.json"), "optimize", "--data", str(workspace / "data"), "--strategy", "breakout",
                 "--folds", "2", "--jobs", "1", "--min-trades", "1", "-o", str(out), "--write-config", str(workspace / "tuned.json")])  # fmt: skip
    assert code == 0
    assert "Out-of-sample results only" in (out / "walkforward_breakout_M15.html").read_text(encoding="utf-8")
    tuned = json.loads((workspace / "tuned.json").read_text())
    assert tuned["strategy"]["name"] == "breakout" and tuned["dry_run"] is True


def test_config_errors_exit_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"risk": {"risk_percent": 50}}')
    assert main(["-c", str(bad), "backtest"]) == 2
    assert "risk_percent" in capsys.readouterr().out


def test_symbol_subset_with_shipped_config(tmp_path, monkeypatch):
    """Regression: --symbols on the shipped config must not trip over overrides for other symbols."""
    data = tmp_path / "demo"
    data.mkdir()
    synthetic_bars(n=3000, seed=8).to_csv(data / "EURUSD_M15.csv", index=False)
    monkeypatch.chdir(ROOT)
    args = [
        "backtest",
        "--data",
        str(data),
        "--timeframe",
        "M15",
        "--symbols",
        "EURUSD",
        "-o",
        str(tmp_path / "out"),
    ]
    assert main(args) == 0


def test_output_never_crashes_on_legacy_windows_consoles(monkeypatch):
    """Regression (found by Windows CI): cp1252 consoles cannot encode '→' and the CLI crashed."""
    import io
    import sys

    from tradebot.cli import safe_streams

    raw = io.BytesIO()
    legacy = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", legacy)
    safe_streams()
    legacy.write("2024-01-01 → 2024-02-01 · efficiency ≥ 0.5 ✓")
    legacy.flush()
    # Known symbols become readable ASCII; cp1252 keeps '·'; anything else degrades to '?'.
    assert raw.getvalue() == b"2024-01-01 -> 2024-02-01 \xb7 efficiency >= 0.5 ?"


def test_backtest_output_survives_a_cp1252_pipe(tmp_path):
    """End to end: the real CLI, stdout encoded as cp1252 (what Windows uses for redirected output)."""
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"
    subprocess.run(
        [sys.executable, str(repo / "scripts" / "make_demo_data.py"), "--out", str(data), "--bars", "4000"],
        check=True,
        capture_output=True,
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    result = subprocess.run(
        [sys.executable, "-m", "tradebot.cli", "backtest", "--data", str(data),
         "--symbols", "EURUSD", "--timeframe", "M15", "-o", str(tmp_path / "reports")],
        capture_output=True,
        env=env,
        cwd=repo,
    )  # fmt: skip
    output = result.stdout.decode("cp1252")
    assert result.returncode == 0, result.stderr.decode("cp1252", errors="replace")
    assert " -> " in output
    assert "Traceback" not in output


# --- dashboard port -------------------------------------------------------------------------------


class _Ports:
    """Stands in for DashboardServer: the ports in ``taken`` refuse to bind."""

    def __init__(self, taken: set[int]) -> None:
        self.taken, self.tried = taken, []

    def __call__(self, monitor, port: int):
        self.tried.append(port)
        if port in self.taken:
            raise OSError(10013, "access forbidden")
        return port


def test_dashboard_takes_the_next_free_port_when_the_default_is_taken(capsys):
    from tradebot.cli import DEFAULT_PORT, PORT_TRIES, open_dashboard

    free = _Ports(set())
    assert open_dashboard(None, None, free) == DEFAULT_PORT and free.tried == [DEFAULT_PORT]
    busy = _Ports({DEFAULT_PORT, DEFAULT_PORT + 1})
    assert open_dashboard(None, None, busy) == DEFAULT_PORT + 2
    assert busy.tried == [DEFAULT_PORT, DEFAULT_PORT + 1, DEFAULT_PORT + 2]
    full = _Ports(set(range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES)))
    with pytest.raises(ValueError, match="cannot listen on any port from 8765 to 8774"):
        open_dashboard(None, None, full)
    assert len(full.tried) == PORT_TRIES


def test_a_port_asked_for_by_name_is_used_or_refused():
    from tradebot.cli import open_dashboard

    assert open_dashboard(None, 9001, _Ports(set())) == 9001
    chosen = _Ports({9001})
    with pytest.raises(ValueError, match=r"cannot listen on port 9001 .*leave --port out"):
        open_dashboard(None, 9001, chosen)
    assert chosen.tried == [9001]  # never silently moved to another port
