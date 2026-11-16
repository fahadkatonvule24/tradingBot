"""WhatsApp alerts through CallMeBot: what is sent, how often, and that the API key never leaks."""

from __future__ import annotations

import io
import os
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import replace

import pytest
from test_advisor_ui import SYMBOL, features, primed
from test_alerts import bare_config, broker, cfg, fake, forming_sell  # noqa: F401  (fixtures)

from tradebot.cli import main
from tradebot.config import ConfigError, config_from_dict, load_config
from tradebot.notify import (
    WHATSAPP_MAX_CHARS,
    Alerts,
    Notifier,
    WhatsApp,
    WhatsAppError,
    chunks,
    normalize_phone,
    whatsapp_call,
    whatsapp_recipients,
)
from tradebot.ui.monitor import Monitor

KEY = "SECRET-KEY-123"


def queued(phone: str) -> str:
    return f"Message to: {phone} Text to send: x Message queued. You will receive it in a few seconds."


def recording(calls: list, body: str = "Message queued. You will receive it in a few seconds."):
    def opener(request, timeout):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        calls.append({k: v[0] for k, v in query.items()})
        return io.BytesIO(body.encode())

    return opener


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+256 700 123 456", "+256700123456"),
        ("00256700123456", "+256700123456"),
        ("+256-700-123-456", "+256700123456"),
        ("0700123456", None),  # a local number has no country code
        ("256700123456", None),
        ("+12", None),
        ("", None),
        (None, None),
    ],
)
def test_phone_numbers_need_the_country_code(raw, expected):
    assert normalize_phone(raw) == expected


def test_one_message_goes_to_callmebot_with_phone_text_and_key():
    calls: list = []
    whatsapp_call("+256700123456", KEY, "SELL · GBPUSD\nline two", recording(calls))
    assert calls == [{"phone": "+256700123456", "text": "SELL · GBPUSD\nline two", "apikey": KEY}]


def test_errors_never_carry_the_key(caplog):
    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    def offline(request, timeout):
        raise urllib.error.URLError(f"no route to https://api.callmebot.com/whatsapp.php?apikey={KEY}")

    with pytest.raises(WhatsAppError) as error:
        whatsapp_call("+256700123456", KEY, "hi", refused)
    assert "403" in str(error.value) and KEY not in str(error.value)
    with pytest.raises(WhatsAppError, match="rejected the API key"):
        whatsapp_call("+256700123456", KEY, "hi", recording([], "APIKey is invalid. Please check it."))
    with caplog.at_level("WARNING"):
        WhatsApp("+256700123456", KEY, opener=offline, background=False).send("hi", "signal")  # no raise
    assert "WhatsApp alert not sent: could not reach CallMeBot (URLError)" in caplog.text
    assert KEY not in caplog.text


def test_important_level_sends_only_what_is_worth_acting_on():
    calls: list = []
    w = WhatsApp("+256700123456", KEY, "important", opener=recording(calls), background=False)
    for kind in ("signal", "failed", "exit", "position", "news", "order"):
        w.send(kind, kind)
    for kind in ("forming", "blocked", "trend", "analysis", "info"):
        w.send(kind, kind)
    assert [c["text"] for c in calls] == ["signal", "failed", "exit", "position", "news", "order"]
    everything: list = []
    WhatsApp("+256700123456", KEY, "all", opener=recording(everything), background=False).send("x", "forming")
    assert len(everything) == 1


def test_off_without_both_settings_or_with_a_local_number(caplog):
    assert not WhatsApp(None, KEY).enabled and not WhatsApp("+256700123456", None).enabled
    with caplog.at_level("WARNING"):
        assert not WhatsApp("0700123456", KEY, background=False).enabled
    assert "international number" in caplog.text


def test_alerts_close_together_go_out_as_one_message():
    calls: list = []
    sent = threading.Event()

    def opener(request, timeout):
        recording(calls)(request, timeout)
        sent.set()
        return io.BytesIO(b"ok")

    w = WhatsApp("+256700123456", KEY, opener=opener, gather_seconds=0.3, gap_seconds=0)
    w.send("SELL · GBPUSD", "signal")
    w.send("NEWS · USD", "news")
    assert sent.wait(5)
    time.sleep(0.1)
    w.close()
    assert [c["text"] for c in calls] == ["SELL · GBPUSD\n\nNEWS · USD"]


def test_long_text_is_split_at_line_breaks():
    lines = [f"line {i:03d} " + "x" * 90 for i in range(40)]
    parts = chunks("\n".join(lines), WHATSAPP_MAX_CHARS)
    assert all(len(p) <= WHATSAPP_MAX_CHARS for p in parts) and len(parts) > 1
    assert "\n".join(parts) == "\n".join(lines)
    assert chunks("y" * 3100, 1500) == ["y" * 1500, "y" * 1500, "y" * 100]


def test_alerts_fan_out_and_only_telegram_takes_commands():
    telegram: list = []
    whatsapp: list = []
    tg = Notifier("t", "1", background=False, opener=lambda *a, **k: None)
    tg.send = lambda text, kind="info": telegram.append(text)
    alerts = Alerts([tg, WhatsApp("+256700123456", KEY, opener=recording(whatsapp), background=False)])
    assert alerts.enabled and alerts.names == ["Telegram", "WhatsApp +256…456"]
    alerts.send("FORMING", "forming")
    alerts.send("BUY", "signal")
    assert telegram == ["FORMING", "BUY"] and [c["text"] for c in whatsapp] == ["BUY"]
    listened: list = []
    tg.listen = lambda handler, poll_seconds=25: listened.append(handler)
    alerts.listen(str.upper)
    assert listened == [str.upper]
    assert not Alerts.from_env({}).enabled
    only_whatsapp = Alerts.from_env(
        {"WHATSAPP_PHONE": "+256700123456", "WHATSAPP_APIKEY": KEY}, background=False
    )
    assert only_whatsapp.names == ["WhatsApp +256…456"]


def test_several_numbers_each_with_its_own_key(caplog):
    env = {"WHATSAPP_PHONE": "+256709000111, +256760000222", "WHATSAPP_APIKEY": "1111111,2222222"}
    assert whatsapp_recipients(env) == [("+256709000111", "1111111"), ("+256760000222", "2222222")]
    calls: list = []
    alerts = Alerts.from_env(env, opener=recording(calls), background=False)
    assert alerts.names == ["WhatsApp +256…111", "WhatsApp +256…222"]
    alerts.send("SELL · GBPUSD\nSell GBPUSD now.", "signal")
    alerts.send("FORMING · GBPUSD", "forming")  # not at the important level, to either number
    assert [(c["phone"], c["apikey"]) for c in calls] == [
        ("+256709000111", "1111111"),
        ("+256760000222", "2222222"),
    ]
    with caplog.at_level("WARNING"):
        assert (
            whatsapp_recipients(
                {"WHATSAPP_PHONE": "+256709000111,+256760000222", "WHATSAPP_APIKEY": "1111111"}
            )
            == []
        )
    assert "2 number(s)" in caplog.text and "1 key(s)" in caplog.text  # never paired with the wrong key
    assert whatsapp_recipients({"WHATSAPP_PHONE": "+256709000111"}) == []
    assert whatsapp_recipients({}) == []


def test_the_dashboard_sends_a_signal_to_whatsapp_but_not_a_forming_trigger(cfg, broker):  # noqa: F811
    calls: list = []
    alerts = Alerts([WhatsApp("+256700123456", KEY, opener=recording(calls), background=False)])
    monitor = Monitor(cfg, broker, [SYMBOL], notifier=alerts)
    forming_sell(monitor, broker, cfg)  # announces a forming sell on the activity panel
    monitor.refresh()
    assert any(e["action"] == "FORMING" for e in monitor.snapshot()["activity"]) and calls == []
    primed(
        Monitor(cfg, broker, [SYMBOL], notifier=alerts), features({"signal": 1, "raw_signal": 1})
    ).refresh()
    (call,) = calls
    assert call["text"].startswith(f"BUY · {SYMBOL}")


def test_the_whatsapp_level_is_checked_in_the_config():
    assert config_from_dict({"alerts": {"whatsapp": "all"}}, env={}).alerts.whatsapp == "all"
    with pytest.raises(ConfigError, match=r"alerts\.whatsapp"):
        config_from_dict({"alerts": {"whatsapp": "loud"}}, env={})
    with pytest.raises(ConfigError, match="unknown key"):
        config_from_dict({"alerts": {"whatsap": "all"}}, env={})


def test_whatsapp_command_walks_through_the_setup(bare_config, monkeypatch, capsys):  # noqa: F811
    for name in ("WHATSAPP_PHONE", "WHATSAPP_APIKEY"):
        monkeypatch.delenv(name, raising=False)
    assert main(["-c", bare_config, "whatsapp"]) == 1
    assert "callmebot.com" in capsys.readouterr().out

    monkeypatch.setenv("WHATSAPP_PHONE", "0700123456")
    monkeypatch.setenv("WHATSAPP_APIKEY", KEY)
    assert main(["-c", bare_config, "whatsapp"]) == 1
    assert "international number" in capsys.readouterr().out

    sent: list = []
    monkeypatch.setenv("WHATSAPP_PHONE", "+256 700 123 456")
    monkeypatch.setattr(
        "tradebot.notify.whatsapp_call", lambda phone, key, text: sent.append((phone, text)) or queued(phone)
    )
    assert main(["-c", bare_config, "whatsapp"]) == 0
    out = capsys.readouterr().out
    assert sent and sent[0][0] == "+256700123456" and "test message" in sent[0][1]
    assert "CallMeBot queued a test message for +256" in out and "Message queued" in out
    assert KEY not in out and "+256700123456" not in out

    sent.clear()
    monkeypatch.setenv("WHATSAPP_PHONE", "+256709000111,0760000222")
    monkeypatch.setenv("WHATSAPP_APIKEY", "1111111,2222222")
    assert main(["-c", bare_config, "whatsapp"]) == 1  # the second number has no country code
    out = capsys.readouterr().out
    assert [p for p, _ in sent] == ["+256709000111"] and "0760000222" in out and "international" in out

    sent.clear()
    monkeypatch.setenv("WHATSAPP_PHONE", "+256709000111,+256760000222")
    assert main(["-c", bare_config, "whatsapp"]) == 0
    assert [p for p, _ in sent] == ["+256709000111", "+256760000222"]
    assert "2222222" not in capsys.readouterr().out

    monkeypatch.setenv("WHATSAPP_APIKEY", "1111111")
    assert main(["-c", bare_config, "whatsapp"]) == 1
    assert "same number of entries" in capsys.readouterr().out


def test_alerts_section_defaults_keep_other_setups_quiet():
    loaded = config_from_dict({}, env={})
    assert loaded.alerts.other_setups is False and loaded.alerts.whatsapp == "important"
    assert replace(loaded.alerts, other_setups=True).other_setups


def test_a_developers_env_file_never_gives_a_test_real_phone_keys(tmp_path, monkeypatch):
    """conftest guard: the CLI reads .env from the working folder, which may hold live keys."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "WHATSAPP_PHONE=+256709000111\nWHATSAPP_APIKEY=1111111\nTRADEBOT_TEST_MARKER=read\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    try:
        load_config(tmp_path / "config.json")
        assert os.environ.get("TRADEBOT_TEST_MARKER") == "read"  # the file was read
        assert "WHATSAPP_PHONE" not in os.environ and "WHATSAPP_APIKEY" not in os.environ
        assert not Alerts.from_env().enabled
    finally:
        os.environ.pop("TRADEBOT_TEST_MARKER", None)


def test_a_sample_alert_goes_to_every_number_marked_as_a_sample(bare_config, monkeypatch, capsys):  # noqa: F811
    sent: list = []
    monkeypatch.setenv("WHATSAPP_PHONE", "+256709000111,+256760000222")
    monkeypatch.setenv("WHATSAPP_APIKEY", "1111111,2222222")
    monkeypatch.setattr(
        "tradebot.notify.whatsapp_call", lambda phone, key, text: sent.append((phone, text)) or queued(phone)
    )
    assert main(["-c", bare_config, "whatsapp", "--sample"]) == 0
    assert [p for p, _ in sent] == ["+256709000111", "+256760000222"]
    text = sent[0][1]
    assert text.startswith("SAMPLE ALERT") and "do not trade it" in text and "hit its stop" in text
    assert "\nSELL · GBPUSD\n" in text and len(text) <= 1500  # the real format, in one message
    out = capsys.readouterr().out
    assert "queued a sample alert for +256" in out and "+256709000111" not in out  # the number is masked
    assert "Queued is not delivered" in out

    silent: list = []
    monkeypatch.setattr(
        "tradebot.notify.whatsapp_call", lambda phone, key, text: silent.append(phone) or "Busy, try later"
    )
    assert main(["-c", bare_config, "whatsapp", "--sample"]) == 1
    assert "did not say it queued" in capsys.readouterr().out and len(silent) == 2
