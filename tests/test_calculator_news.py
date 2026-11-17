"""The margin and leverage calculator, the news feed, and trades placed on the phone counted from MT5."""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import FakeMT5
from test_advisor_ui import NOW, SYMBOL, features

from tradebot import calculator
from tradebot.book import Book, BookBroker
from tradebot.broker.mt5 import MT5Broker
from tradebot.calculator import CalcError, calculate
from tradebot.cli import main
from tradebot.config import BotConfig, MT5Login, ScheduleParams
from tradebot.engine import Engine, Rejection
from tradebot.news import NewsCalendar, NewsError, NewsFeed
from tradebot.review import closed_trades
from tradebot.ui.desk import calc_request, news_text
from tradebot.ui.monitor import Monitor
from tradebot.ui.server import TOKEN_HEADER, DashboardServer

URL = "https://example.test/ff_calendar_thisweek.json"
FEED = [  # the feed's own format: New York times with their offset, ForexFactory field names
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-03-04T08:30:00-05:00",
     "impact": "High", "forecast": "120K", "previous": "98K"},
    {"title": "German ZEW Economic Sentiment", "country": "EUR", "date": "2026-03-04T05:00:00-05:00",
     "impact": "Medium", "forecast": "", "previous": "10.0"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-03-04T00:00:00-05:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
]  # fmt: skip


@pytest.fixture
def fake() -> FakeMT5:
    return FakeMT5(now=NOW)


@pytest.fixture
def broker(fake: FakeMT5) -> MT5Broker:
    return MT5Broker(MT5Login(), offset_hours=2.0, module=fake, clock=lambda: fake.utc_now)


@pytest.fixture
def cfg() -> BotConfig:
    return BotConfig(
        symbols=(SYMBOL,), timeframe="M15", schedule=ScheduleParams(session="all", trade_weekends=True, news_file="")
    )  # fmt: skip


def primed(monitor: Monitor) -> Monitor:
    monitor._features[SYMBOL] = features()
    monitor._fetched[SYMBOL] = time.monotonic()
    monitor._preview_for = lambda symbol, feats: None
    return monitor


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def serve(body: bytes):
    calls: list[str] = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return Response(body)

    return opener, calls


# --- the calculator -------------------------------------------------------------------------------


def test_size_from_risk_and_the_margin_it_needs(broker):
    r = calculate(broker, SYMBOL, 1, stop_pips=20, risk_percent=1.0, margin_cap=30)
    # ask 1.10012; a 20-pip stop costs 200 a lot; 1% of 10,000 = 100 -> 0.5 lots at 10 a pip a lot
    assert (r["stop"], r["lots"], r["lots_how"]) == (1.09812, 0.5, "from the risk")
    assert r["pip_value_per_lot"] == pytest.approx(10.0) and r["pip_value"] == pytest.approx(5.0)
    assert r["risk_amount"] == pytest.approx(100.0) and r["risk_percent"] == pytest.approx(1.0)
    assert r["notional"] == pytest.approx(55_006) and r["effective_leverage"] == pytest.approx(5.5006)
    assert r["margin"] == pytest.approx(550.06) and r["margin_method"] == "the terminal's own figure"
    assert r["margin_level_after"] == pytest.approx(10_000 / 550.06 * 100)
    assert r["max_lots_by_cap"] == 2.72  # 30% of 10,000 is 3,000 of margin, at 1,100.12 a lot
    assert r["max_lots_by_free_margin"] == 9.08
    assert r["pips_to_margin_call"] == pytest.approx((10_000 - 550.06) / 5.0)
    assert r["pips_to_stop_out"] == pytest.approx((10_000 - 275.03) / 5.0)
    at = {row["leverage"]: row for row in r["by_leverage"]}
    assert at[30]["margin"] == pytest.approx(55_006 / 30) and at[500]["max_lots_by_cap"] == 13.63
    other = calculate(broker, SYMBOL, 1, stop_pips=20, risk_percent=1.0, leverage=500, margin_cap=30)
    assert other["lots"] == 0.5 and other["margin"] == pytest.approx(110.012)
    assert other["margin_method"] == "notional / 500" and other["terminal_margin"] == pytest.approx(550.06)


def test_the_calculator_says_when_a_trade_does_not_fit(broker):
    tiny = calculate(broker, SYMBOL, 1, stop_pips=20, risk_percent=0.25, equity=100)
    assert tiny["lots"] == 0
    assert tiny["warnings"][0].startswith("The smallest lot, 0.01, would risk 2.00 (2.00% of equity)")
    big = calculate(broker, SYMBOL, -1, stop_pips=5, lots=5, margin_cap=30)
    assert (
        "Margin would reach 55% of equity, above your cap of 30%: at most 2.72 lots fit." in big["warnings"]
    )
    odd = calculate(broker, SYMBOL, 1, stop_pips=20, lots=0.123)
    assert odd["lots"] == 0.12 and "lot step" in odd["warnings"][0]
    with pytest.raises(CalcError, match="stop of a buy must be below"):
        calculate(broker, SYMBOL, 1, stop=1.2, lots=0.1)
    with pytest.raises(CalcError, match="give a stop"):
        calculate(broker, SYMBOL, 1, risk_percent=1)
    with pytest.raises(CalcError, match="leverage"):
        calculate(broker, SYMBOL, 1, stop_pips=20, lots=0.1, leverage=0.5)


def test_the_dashboard_calculator_starts_from_the_bots_plan(cfg, broker, tmp_path):
    monitor = primed(Monitor(cfg, broker, [SYMBOL], book=Book(tmp_path / "j.sqlite")))
    r = calc_request(monitor, {"symbol": "eurusd", "side": "buy"})
    # the plan: 1.5 ATR stop, 3 ATR target, 0.25% risk; the config's 30% margin cap
    assert r["stops_from_plan"] and (r["stop"], r["target"], r["lots"]) == (1.09862, 1.10312, 0.16)
    assert r["reward_risk"] == pytest.approx(2.0) and r["margin_cap"] == 30
    moved = calc_request(monitor, {"symbol": SYMBOL, "side": 1, "entry": "1.1050", "leverage": "500"})
    assert (moved["stop"], moved["target"]) == (1.1035, 1.108)  # the plan's distances from your price
    text = monitor.desk.command("/calc EURUSD sell risk=1 pips=20 lev=500")
    assert text.startswith(f"SELL {SYMBOL} at 1.10000 (live) · equity 10,000.00 USD · 1:500")
    assert "Size 0.5 lots (from the risk)" in text and "Margin call (100%) after" in text
    assert monitor.desk.command("/calc EURUSD buy sl=1.2").startswith("the stop of a buy must be below")


def test_calc_endpoint_needs_the_session_token(cfg, broker):
    monitor = primed(Monitor(cfg, broker, [SYMBOL]))
    worker = threading.Thread(target=monitor.run, daemon=True)
    server = DashboardServer(monitor, port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    worker.start()
    try:

        def post(headers: dict) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
            body = json.dumps({"symbol": SYMBOL, "side": 1, "stop_pips": 20, "risk_percent": 1})
            conn.request("POST", "/api/calc", body, {"Host": f"127.0.0.1:{server.port}", **headers})
            response = conn.getresponse()
            return response.status, json.loads(response.read())

        assert post({})[0] == 403
        status, out = post({TOKEN_HEADER: server.token})
        assert status == 200 and out["ok"] and out["result"]["lots"] == 0.5
    finally:
        server.shutdown()
        monitor.stop()
        worker.join(5)


def test_leverage_on_your_own_account_checks_margin(cfg, broker, tmp_path):
    book = Book(tmp_path / "j.sqlite")
    book.save_account("Exness", 2000, NOW, leverage=30)
    book.use("Exness")
    own = BookBroker(broker, book, cfg.magic)
    assert own.margin_required(SYMBOL, 1, 0.1) == pytest.approx(0.1 * 110_012 / 30)
    book.open_trade(
        "Exness", SYMBOL, 1, 0.3, 1.1000, NOW, sl=1.0999
    )  # bounded risk, so this exercises margin
    account = own.account()
    assert account.leverage == 30 and account.margin == pytest.approx(0.3 * 110_000 / 30)
    row = features({"signal": 1, "raw_signal": 1}).iloc[-1]
    plan = Engine(cfg, own, execute=False).plan_entry(
        SYMBOL, own.symbol(SYMBOL), cfg.for_symbol(SYMBOL), row, 0.00012, 1
    )
    assert isinstance(plan, Rejection) and plan.message == "margin would reach 61%"
    book.save_account("Exness", 2000, NOW)  # leverage removed: margin is not checked
    assert own.margin_required(SYMBOL, 1, 0.1) is None


def test_a_book_made_before_leverage_existed_is_upgraded(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE book_accounts (name TEXT PRIMARY KEY, balance REAL NOT NULL, min_lot REAL, "
               "lot_step REAL, updated_utc TEXT NOT NULL)")  # fmt: skip
    db.execute("INSERT INTO book_accounts VALUES ('Old', 500, NULL, NULL, '2026-03-01T00:00:00')")
    db.commit()
    db.close()
    book = Book(path)
    assert book.account("Old").leverage is None
    assert book.save_account("Old", 500, NOW, leverage=200).leverage == 200


# --- trades placed by hand, counted from the MT5 account -------------------------------------------


def deal(
    fake, ticket, position, when, entry, profit=0.0, price=1.1000, kind=0, comment="", magic=0, symbol=SYMBOL
):
    return SimpleNamespace(ticket=ticket, position_id=position, symbol=symbol, time=fake.server_epoch(when), type=kind,
                           volume=0.10, price=price, profit=profit, commission=0.0, swap=0.0, fee=0.0, magic=magic,
                           entry=entry, comment=comment)  # fmt: skip


def phone_day(fake) -> None:
    """Two losing trades placed on the phone this morning, and a deposit that is not a trade."""
    t = datetime(2026, 3, 4, 9)
    fake.deals = [
        deal(fake, 1, 11, t, 0),
        deal(fake, 2, 11, t + timedelta(minutes=40), 1, -15.0, 1.0985, kind=1, comment="[sl 1.09850]"),
        deal(fake, 3, 12, t + timedelta(minutes=50), 0),
        deal(fake, 4, 12, t + timedelta(minutes=90), 1, -15.0, 1.0985, kind=1, comment="[sl 1.09850]"),
        deal(fake, 5, 0, t, 0, 5_000.0, 0.0, kind=2, symbol=""),  # a deposit: DEAL_TYPE_BALANCE
    ]
    fake.orders = [
        SimpleNamespace(position_id=p, time_setup=1, volume_initial=0.1, sl=1.0985) for p in (11, 12)
    ]


def test_phone_trades_count_in_the_limits_without_being_logged(cfg, broker, fake):
    phone_day(fake)
    tight = replace(cfg, risk=replace(cfg.risk, daily_loss_limit_percent=0.25, max_consecutive_losses=2))
    status = Engine(tight, broker, execute=False, live=True).risk_status(NOW)
    assert status.day_pnl == pytest.approx(-30.0)  # the deposit is not a profit
    assert status.blocked.startswith("daily loss limit") and status.losing_streak == 2
    only_bot = replace(tight, risk=replace(tight.risk, account_trades=False))
    status = Engine(only_bot, broker, execute=False, live=True).risk_status(NOW)
    assert status.day_pnl == 0 and status.blocked == ""


def test_phone_trades_are_reviewed_in_r_as_your_own_calls(cfg, broker, fake):
    phone_day(fake)
    trades = closed_trades(broker, None, datetime(2026, 3, 4), bot_magic=cfg.magic)
    assert [(t.ticket, t.manual, t.exit, t.volume, t.entry, t.exit_price) for t in trades] == [
        (11, True, "stop", 0.1, 1.1, 1.0985), (12, True, "stop", 0.1, 1.1, 1.0985)]  # fmt: skip
    assert [t.r for t in trades] == [pytest.approx(-1.0), pytest.approx(-1.0)]


def test_the_bot_never_manages_a_position_you_opened(cfg, broker, fake):
    fake.positions = [SimpleNamespace(ticket=21, symbol=SYMBOL, type=0, volume=0.1, price_open=1.0950, sl=1.0935, tp=0.0,
                                      time=fake.server_epoch(NOW - timedelta(hours=2)), profit=50.0, comment="",
                                      magic=0)]  # fmt: skip
    engine = Engine(cfg, broker, execute=True, live=True)
    row = features().iloc[-1]
    engine.on_bar(SYMBOL, broker.symbol(SYMBOL), cfg.for_symbol(SYMBOL), row, 0.00012, NOW)
    assert fake.sent == []  # +3R on someone else's trade: no breakeven, no trail, no close
    status = engine.risk_status(NOW)  # but it counts in the account's limits: 15 pips at 0.1 lots
    assert status.positions == 1 and status.open_risk_percent == pytest.approx(0.15)
    fake.positions[0].sl = 1.0960  # once its stop is past the entry it puts nothing at risk
    assert engine.risk_status(NOW).open_risk_percent == 0


def test_a_stop_added_after_the_trade_opened_still_measures_it(cfg, broker, fake, tmp_path):
    own = BookBroker(broker, Book(tmp_path / "j.sqlite"), cfg.magic)
    fake.orders = [
        SimpleNamespace(position_id=31, time_setup=1, volume_initial=0.1, sl=0.0)
    ]  # no stop at first
    fake.positions = [SimpleNamespace(ticket=31, symbol=SYMBOL, type=0, volume=0.1, price_open=1.1000, sl=1.0985,
                                      tp=0.0, time=fake.server_epoch(NOW), profit=0.0, comment="", magic=0)]  # fmt: skip
    assert own.positions(None)[0].initial_sl == 1.0985
    fake.positions[0].sl = 1.1000  # later moved to breakeven: the first stop seen stays the 1R
    assert own.positions(None)[0].initial_sl == 1.0985
    fake.positions = []
    fake.deals = [deal(fake, 1, 31, NOW, 0), deal(fake, 2, 31, NOW + timedelta(minutes=5), 1, -15.0, 1.0985, kind=1,
                                                comment="[sl 1.09850]")]  # fmt: skip
    (trade,) = closed_trades(own, None, NOW - timedelta(hours=1), bot_magic=cfg.magic)
    assert trade.manual and trade.r == pytest.approx(-1.0)


def test_the_dashboard_reviews_the_mt5_account_without_logging(cfg, broker, fake, tmp_path):
    phone_day(fake)
    monitor = primed(Monitor(cfg, broker, [SYMBOL], book=Book(tmp_path / "j.sqlite")))
    monitor.refresh()
    book = monitor.snapshot()["book"]
    assert book["active"] is None and book["review"]["all"]["trades"] == 2
    assert book["review"]["own"]["avg_r"] == pytest.approx(-1.0)
    assert [(t["id"], t["source"], t["r"]) for t in book["closed"]] == [
        (12, "own call", pytest.approx(-1.0)), (11, "own call", pytest.approx(-1.0))]  # fmt: skip


# --- the news ---------------------------------------------------------------------------------------


def test_the_feed_is_downloaded_into_a_file_the_calendar_reads(tmp_path):
    opener, calls = serve(json.dumps(FEED).encode())
    path = tmp_path / "news_feed.json"
    assert NewsFeed(URL, path, opener=opener).refresh() == 3 and calls == [URL]
    calendar = NewsCalendar(None, path)
    nfp = calendar.events()[-1]
    assert (nfp.time, nfp.currency, nfp.impact, nfp.forecast, nfp.previous) == (
        datetime(2026, 3, 4, 13, 30), "USD", "high", "120K", "98K")  # fmt: skip
    assert calendar.blackout(datetime(2026, 3, 4, 13, 10), {"EUR", "USD"}, 30, 30) == nfp
    assert (
        calendar.blackout(datetime(2026, 3, 4, 9, 50), {"EUR", "USD"}, 30, 30) is None
    )  # medium never pauses
    assert [e.title for e in calendar.upcoming(datetime(2026, 3, 4, 9), {"EUR", "USD"})] == [
        "German ZEW Economic Sentiment", "Non-Farm Employment Change"]  # fmt: skip
    own = tmp_path / "own.json"
    own.write_text(json.dumps([{"time_utc": "2026-03-04T13:30:00Z", "currency": "USD", "impact": "high",
                                "event": "Non-Farm Employment Change"}]), encoding="utf-8")  # fmt: skip
    assert len(NewsCalendar(own, path).events()) == 3  # the same event in both files counts once


@pytest.mark.parametrize(
    "answer, message",
    [
        (urllib.error.HTTPError(URL, 429, "Too Many Requests", {}, None), r"answered 429 \(asked too often"),
        (b"<html>maintenance</html>", "not JSON"),
        (b'[{"what": 1}]', "format has changed"),
    ],
)
def test_a_failed_download_keeps_the_last_calendar(tmp_path, answer, message):
    path = tmp_path / "news_feed.json"
    path.write_text("[]", encoding="utf-8")

    def opener(request, timeout):
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)

    with pytest.raises(NewsError, match=message):
        NewsFeed(URL, path, opener=opener).refresh()
    assert path.read_text(encoding="utf-8") == "[]"


def test_the_dashboard_lists_the_news_and_warns_once_before_it(cfg, broker, fake, tmp_path):
    path = tmp_path / "news_feed.json"
    NewsFeed(URL, path, opener=serve(json.dumps(FEED).encode())[0]).refresh()
    cfg = replace(cfg, schedule=replace(cfg.schedule, news_feed=URL, news_feed_file=str(path)))
    inbox: list[str] = []
    monitor = primed(
        Monitor(
            cfg, broker, [SYMBOL], notifier=SimpleNamespace(send=lambda text, kind="info": inbox.append(text))
        )
    )
    monitor.refresh()
    news = monitor.snapshot()["market"]["news"]
    assert news["on"] and [e["title"] for e in news["events"]] == ["Non-Farm Employment Change"]
    assert news["events"][0]["symbols"] == [SYMBOL] and not news["events"][0]["paused_now"]
    assert inbox == []  # 83 minutes away: too early to warn
    fake.utc_now = datetime(2026, 3, 4, 13, 5)
    monitor._fetched[SYMBOL] = 0.0  # fresh bars for the new time
    monitor.refresh()
    monitor.refresh()
    (message,) = [m for m in inbox if m.startswith("NEWS")]
    assert message == (
        "NEWS · USD\nUSD Non-Farm Employment Change in 25 min (13:30 UTC, high impact; forecast 120K, previous "
        f"98K). New entries are paused on {SYMBOL} from 13:00 to 14:00 UTC. Spreads widen and stops can fill "
        "worse around a release."
    )
    state = monitor.snapshot()
    assert state["market"]["news"]["events"][0]["paused_now"]
    assert state["symbols"][0]["advice"]["blocker"].startswith("news: USD Non-Farm Employment Change")
    assert news_text(monitor, fake.utc_now) == (
        "Wed 13:30 UTC · USD HIGH · Non-Farm Employment Change (f 120K · p 98K) · ENTRIES PAUSED"
    )


def test_news_command_lists_your_instruments_news(tmp_path, capsys):
    soon = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)
    own = tmp_path / "own.json"
    own.write_text(json.dumps([
        {"time_utc": soon.isoformat(), "currency": "USD", "impact": "high", "event": "CPI m/m"},
        {"time_utc": soon.isoformat(), "currency": "CHF", "impact": "high", "event": "SNB Rate"},
    ]), encoding="utf-8")  # fmt: skip
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"symbols": ["EURUSD"], "schedule": {"news_file": str(own)}}), encoding="utf-8"
    )
    assert main(["-c", str(config), "news"]) == 0
    out = capsys.readouterr().out
    assert "The news feed is off" in out and "CPI m/m" in out and "SNB Rate" not in out  # not your currencies


def test_lots_that_risk_more_than_the_config_allows_are_flagged(cfg, broker, tmp_path):
    monitor = primed(Monitor(cfg, broker, [SYMBOL]))
    r = calc_request(monitor, {"symbol": SYMBOL, "side": 1, "lots": 3})
    assert (
        "At the stop this loses 4.50% of equity, more than the 0.25% a trade in your config."
        in r["warnings"]  # 3 lots x 15 pips
    )
    assert calculator.LEVERAGES[0] == 10 and r["leverages"][-1] == 2000


def test_unlimited_leverage_as_exness_reports_it(broker, fake):
    """Exness 'unlimited' leverage arrives as about two billion; the calculator must not refuse it."""
    account = fake.account_info()
    fake.account_info = lambda: SimpleNamespace(**{**vars(account), "leverage": 2_147_483_647})
    r = calculate(broker, SYMBOL, 1, stop_pips=20, risk_percent=1.0, margin_cap=30)
    assert r["lots"] == 0.5 and r["leverage_label"] == "unlimited"
    assert r["margin_method"] == "the terminal's own figure"  # the broker's figure, not notional / 2e9
    assert "· 1:unlimited" in calculator.summary(r)
    chosen = calculate(broker, SYMBOL, 1, stop_pips=20, lots=0.5, leverage=200)
    assert chosen["margin"] == pytest.approx(55_006 / 200) and chosen["account_leverage_label"] == "unlimited"


def test_the_feed_thread_survives_an_unexpected_download_error(tmp_path):
    """Regression: an error outside the expected ones (a cut-off reply) ended the download thread,
    and the calendar was never refreshed again until a restart."""
    import http.client
    import threading
    from datetime import datetime

    from tradebot.news import NewsFeed

    calls, second = [], threading.Event()

    def opener(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"")
        second.set()
        raise OSError("offline")

    feed = NewsFeed("https://example.invalid/feed.json", tmp_path / "feed.json", opener=opener,
                    clock=lambda: datetime(2026, 11, 17, 12))  # fmt: skip
    import tradebot.news as news

    retry, news.FEED_RETRY = news.FEED_RETRY, news.timedelta(seconds=0.05)
    try:
        feed.start()
        assert second.wait(5), "the thread died after the first error"
        assert feed.error  # the failure is reported on the page, and the thread carried on
    finally:
        feed.stop()
        news.FEED_RETRY = retry
