"""Alerts to your phone through a Telegram bot.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (environment or .env, never the committed config)
and the dashboard and the trading loop send a message when a signal fires, when a filter blocks
a trigger, when a trigger is forming, and when an order is placed. `tradebot telegram` walks
through the setup and sends a test message.

Messages are sent from a background thread so a slow network never delays trading, failures are
logged and dropped, and the token is never written to a log.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
TOKEN_ENV, CHAT_ENV = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"


class TelegramError(RuntimeError):
    pass


def call(token: str, method: str, params: dict[str, Any], opener: Callable = urllib.request.urlopen) -> Any:
    """One Bot API call. Errors never include the URL, because the URL contains the token."""
    request = urllib.request.Request(
        API.format(token=token, method=method), data=urllib.parse.urlencode(params).encode(), method="POST"
    )
    try:
        with opener(request, timeout=10) as response:
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

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **kwargs: Any) -> Notifier:
        env = dict(os.environ) if env is None else env
        return cls(env.get(TOKEN_ENV) or None, env.get(CHAT_ENV) or None, **kwargs)

    def send(self, text: str) -> None:
        """Queue a message. Never raises and never blocks the caller."""
        if not self.enabled:
            return
        if not self._background:
            self._deliver(text)
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            log.warning("Telegram queue is full; dropping an alert")

    def close(self, timeout: float = 5.0) -> None:
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
