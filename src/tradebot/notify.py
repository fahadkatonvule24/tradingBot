"""Alerts to your phone: WhatsApp, a Telegram bot, or both.

Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (environment or .env, never the committed
config). It gets every alert and also answers commands such as /plan and /calc. `tradebot
telegram` walks through the setup and sends a test message.

WhatsApp: set WHATSAPP_PHONE and WHATSAPP_APIKEY from CallMeBot's free personal API. Several
numbers are comma-separated, each key in the same position as its number (CallMeBot gives every
number its own key). It sends only, so no commands, and by default only the alerts worth acting
on (``alerts.whatsapp``). `tradebot whatsapp` walks through the setup and sends a test message.

Messages are sent from background threads so a slow network never delays trading, failures are
logged and dropped, and neither the token nor the API key is ever written to a log.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
TOKEN_ENV, CHAT_ENV = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"

WHATSAPP_API = "https://api.callmebot.com/whatsapp.php"
PHONE_ENV, WHATSAPP_KEY_ENV = "WHATSAPP_PHONE", "WHATSAPP_APIKEY"
WHATSAPP_MAX_CHARS = 1500
"""One WhatsApp message at most; the text travels in the request URL."""

IMPORTANT = frozenset({"signal", "failed", "exit", "position", "news", "order"})
"""Alert kinds a channel set to "important" sends: a confirmed buy or sell, a failed break,
get-out and reversal warnings and assumed closes on open positions, news, and orders. The rest
("forming", "blocked", "trend", "analysis", "info") are on the dashboard and in Telegram."""


class TelegramError(RuntimeError):
    pass


def call(
    token: str,
    method: str,
    params: dict[str, Any],
    opener: Callable = urllib.request.urlopen,
    timeout: float = 10,
) -> Any:
    """One Bot API call. Errors never include the URL, because the URL contains the token."""
    request = urllib.request.Request(
        API.format(token=token, method=method), data=urllib.parse.urlencode(params).encode(), method="POST"
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        reason = {401: "the bot token was rejected", 400: "bad request (is the chat id right?)",
                  403: "the bot cannot message this chat (send it /start first)"}.get(exc.code, exc.reason)  # fmt: skip
        raise TelegramError(f"Telegram answered {exc.code}: {reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"could not reach Telegram ({type(exc).__name__})") from None
    except ValueError:
        raise TelegramError("Telegram sent an unreadable answer") from None
    if not payload.get("ok"):
        raise TelegramError(f"Telegram refused: {payload.get('description', 'no reason given')}")
    return payload.get("result")


class Notifier:
    """Queue of outgoing messages. ``enabled`` is False when no credentials are configured."""

    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        opener: Callable = urllib.request.urlopen,
        background: bool = True,
    ) -> None:
        self.token, self.chat_id, self._opener = token, chat_id, opener
        self.enabled = bool(token and chat_id)
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None
        if self.enabled and background:
            self._thread = threading.Thread(target=self._drain, name="telegram", daemon=True)
            self._thread.start()
        self._background = background
        self._listener: threading.Thread | None = None
        self._handler: Callable[[str], str] | None = None
        self._poll = 25
        self._closing = threading.Event()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **kwargs: Any) -> Notifier:
        env = dict(os.environ) if env is None else env
        return cls(env.get(TOKEN_ENV) or None, env.get(CHAT_ENV) or None, **kwargs)

    def send(self, text: str, kind: str = "info") -> None:
        """Queue a message. Never raises and never blocks the caller. Telegram gets every kind."""
        if not self.enabled:
            return
        if not self._background:
            self._deliver(text)
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            log.warning("Telegram queue is full; dropping an alert")

    def listen(self, handler: Callable[[str], str], poll_seconds: int = 25) -> None:
        """Answer commands sent to the bot from the configured chat, and from nowhere else.

        ``handler`` gets the message text and returns the reply ("" for no reply). Messages that
        were waiting before this started are skipped, so an old command is never replayed.
        """
        if not self.enabled or self._listener:
            return
        self._handler, self._poll = handler, poll_seconds
        self._listener = threading.Thread(target=self._listen, name="telegram-in", daemon=True)
        self._listener.start()

    def _updates(self, offset: int, wait: int) -> list[dict[str, Any]]:
        return call(self.token or "", "getUpdates", {"offset": offset, "timeout": wait},
                    self._opener, timeout=wait + 10) or []  # fmt: skip

    def _listen(self) -> None:
        offset = self._skip_backlog()
        while not self._closing.is_set():
            try:
                updates = self._updates(offset, self._poll)
            except TelegramError as exc:
                log.debug("Telegram commands: %s", exc)
                self._closing.wait(15)
                continue
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                self.handle(update)

    def _skip_backlog(self) -> int:
        try:
            waiting = self._updates(-1, 0)
        except TelegramError:
            return 0
        return int(waiting[-1].get("update_id", 0)) + 1 if waiting else 0

    def handle(self, update: dict[str, Any]) -> None:
        """One incoming update. Only a /command from the configured chat is acted on."""
        message = update.get("message") or {}
        text = str(message.get("text") or "")
        if str((message.get("chat") or {}).get("id")) != str(self.chat_id) or not text.startswith("/"):
            return
        try:
            reply = self._handler(text) if self._handler else ""
        except Exception:
            log.exception("Telegram command failed")
            reply = "That command failed; the details are in tradebot.log."
        if reply:
            self.send(reply)

    def close(self, timeout: float = 5.0) -> None:
        self._closing.set()
        if self._thread:
            self._queue.put(None)
            self._thread.join(timeout)

    def _drain(self) -> None:
        while (text := self._queue.get()) is not None:
            self._deliver(text)

    def _deliver(self, text: str) -> bool:
        try:
            call(
                self.token or "", "sendMessage", {"chat_id": self.chat_id, "text": text[:4000]}, self._opener
            )
        except TelegramError as exc:
            log.warning("Telegram alert not sent: %s", exc)
            return False
        return True


def discover_chats(token: str, opener: Callable = urllib.request.urlopen) -> list[tuple[str, str]]:
    """(chat id, name) of everyone who has messaged the bot recently: how you find your chat id."""
    seen: dict[str, str] = {}
    for update in call(token, "getUpdates", {}, opener) or []:
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            )
            seen[str(chat["id"])] = name or chat.get("username") or "unnamed chat"
    return list(seen.items())


class WhatsAppError(RuntimeError):
    pass


def whatsapp_call(
    phone: str, apikey: str, text: str, opener: Callable = urllib.request.urlopen, timeout: float = 20
) -> str:
    """Send one WhatsApp message to your own number through CallMeBot and return its reply as
    plain text, e.g. "Message queued. You will receive it in a few seconds." Queued is not
    delivered: CallMeBot sends from its own queue. Errors never include the URL, because the URL
    contains the API key."""
    query = urllib.parse.urlencode({"phone": phone, "text": text, "apikey": apikey})
    request = urllib.request.Request(f"{WHATSAPP_API}?{query}", headers={"User-Agent": "tradebot alerts"})
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise WhatsAppError(f"CallMeBot answered {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WhatsAppError(f"could not reach CallMeBot ({type(exc).__name__})") from None
    if "apikey" in body.lower() and ("invalid" in body.lower() or "not valid" in body.lower()):
        raise WhatsAppError("CallMeBot rejected the API key (check WHATSAPP_APIKEY and WHATSAPP_PHONE)")
    reply = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    log.debug("CallMeBot: %s", reply.replace(apikey, "<key>") if apikey else reply)
    return reply.replace(apikey, "<key>") if apikey else reply


def normalize_phone(phone: str | None) -> str | None:
    """+256 700 123 456 or 00256 700 123 456 -> +256700123456. None unless the number starts with
    + or 00 (a local number such as 0700 123 456 has no country code)."""
    raw = (phone or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("00"):
        digits = digits[2:]
    elif not raw.startswith("+"):
        return None
    return f"+{digits}" if 8 <= len(digits) <= 15 else None


class _Channel:
    """A send-only channel: picks alert kinds by level and delivers from a background queue.

    ``gather_seconds`` > 0 merges alerts that arrive close together into one message (subclasses
    say how, in ``_merge``); ``gap_seconds`` spaces deliveries out.
    """

    name = "channel"

    def __init__(
        self, enabled: bool, level: str, background: bool, gather_seconds: float, gap_seconds: float
    ) -> None:
        self.enabled, self.level = enabled, level
        self._gather, self._gap = gather_seconds, gap_seconds
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=100)
        self._closing = threading.Event()
        self._background = background
        self._thread: threading.Thread | None = None
        if self.enabled and background:
            self._thread = threading.Thread(target=self._drain, name=self.name.lower(), daemon=True)
            self._thread.start()

    def wants(self, kind: str) -> bool:
        return self.level == "all" or kind in IMPORTANT

    def send(self, text: str, kind: str = "info") -> None:
        """Queue a message of this kind. Never raises and never blocks the caller."""
        if not self.enabled or not self.wants(kind):
            return
        if not self._background:
            for part, part_kind in self._merge([(text, kind)]):
                self._deliver(part, part_kind)
            return
        try:
            self._queue.put_nowait((text, kind))
        except queue.Full:
            log.warning("%s queue is full; dropping an alert", self.name)

    def close(self, timeout: float = 5.0) -> None:
        self._closing.set()
        if self._thread:
            self._queue.put(None)
            self._thread.join(timeout)

    def _drain(self) -> None:
        while (item := self._queue.get()) is not None:
            batch, done = [item], False
            while self._gather > 0 and not done and not self._closing.is_set():
                try:
                    more = self._queue.get(timeout=self._gather)
                except queue.Empty:
                    break
                if more is None:
                    done = True
                else:
                    batch.append(more)
            for text, kind in self._merge(batch):
                self._deliver(text, kind)
                self._closing.wait(self._gap)
            if done:
                return

    def _merge(self, batch: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return batch

    def _deliver(self, text: str, kind: str) -> bool:
        raise NotImplementedError


class WhatsApp(_Channel):
    """Alerts to your own WhatsApp through CallMeBot's free personal API (send only).

    Alerts that arrive within a few seconds of each other go out as one message, and messages are
    spaced out, because the free service does not publish its rate limits.
    """

    name = "WhatsApp"

    def __init__(
        self,
        phone: str | None,
        apikey: str | None,
        level: str = "important",
        opener: Callable = urllib.request.urlopen,
        background: bool = True,
        gather_seconds: float = 3.0,
        gap_seconds: float = 10.0,
    ) -> None:
        self.phone, self.apikey, self._opener = normalize_phone(phone), apikey, opener
        self.label = f"WhatsApp {masked(self.phone)}" if self.phone else "WhatsApp"
        if phone and not self.phone:
            log.warning(
                "WhatsApp alerts off: %s needs the full international number, e.g. +256700123456", PHONE_ENV
            )
        super().__init__(bool(self.phone and apikey), level, background, gather_seconds, gap_seconds)

    def _merge(self, batch: list[tuple[str, str]]) -> list[tuple[str, str]]:
        joined = "\n\n".join(text for text, _ in batch)
        return [(part, "signal") for part in chunks(joined, WHATSAPP_MAX_CHARS)]

    def _deliver(self, text: str, kind: str = "info") -> bool:
        try:
            whatsapp_call(self.phone or "", self.apikey or "", text, self._opener)
        except WhatsAppError as exc:
            log.warning("WhatsApp alert not sent: %s", exc)
            return False
        return True


def masked(phone: str) -> str:
    """+256700123456 -> +256…456: enough to tell your numbers apart in a log."""
    return f"{phone[:4]}…{phone[-3:]}"


def whatsapp_recipients(env: dict[str, str]) -> list[tuple[str, str]]:
    """(number, key) pairs from WHATSAPP_PHONE and WHATSAPP_APIKEY: one of each, or several
    comma-separated in the same order. A count that does not match turns WhatsApp off, because
    pairing a number with another number's key would fail silently."""
    phones = [p.strip() for p in (env.get(PHONE_ENV) or "").split(",") if p.strip()]
    keys = [k.strip() for k in (env.get(WHATSAPP_KEY_ENV) or "").split(",") if k.strip()]
    if phones and keys and len(phones) != len(keys):
        log.warning(
            "WhatsApp alerts off: %s has %d number(s) and %s has %d key(s); give one key per number, "
            "in the same order",
            PHONE_ENV, len(phones), WHATSAPP_KEY_ENV, len(keys),
        )  # fmt: skip
        return []
    return list(zip(phones, keys, strict=False))


def chunks(text: str, size: int) -> list[str]:
    """Split at line breaks into pieces of at most ``size`` characters."""
    out: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > size:
            if current:
                out.append(current)
                current = ""
            out.append(line[:size])
            line = line[size:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > size:
            out.append(current)
            current = line
        else:
            current = candidate
    if current:
        out.append(current)
    return out


class Alerts:
    """Every configured channel. Telegram also takes commands; WhatsApp only sends."""

    def __init__(self, channels: list[Any]) -> None:
        self.channels = [c for c in channels if c.enabled]

    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, whatsapp_level: str = "important", **kwargs: Any
    ) -> Alerts:
        env = dict(os.environ) if env is None else env
        numbers = [WhatsApp(phone, key, whatsapp_level, **kwargs) for phone, key in whatsapp_recipients(env)]
        return cls([Notifier.from_env(env, **kwargs), *numbers])

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    @property
    def names(self) -> list[str]:
        return ["Telegram" if isinstance(c, Notifier) else c.label for c in self.channels]

    def send(self, text: str, kind: str = "info") -> None:
        for channel in self.channels:
            channel.send(text, kind)

    def listen(self, handler: Callable[[str], str], poll_seconds: int = 25) -> None:
        for channel in self.channels:
            if isinstance(channel, Notifier):
                channel.listen(handler, poll_seconds)

    def close(self, timeout: float = 5.0) -> None:
        for channel in self.channels:
            channel.close(timeout)
