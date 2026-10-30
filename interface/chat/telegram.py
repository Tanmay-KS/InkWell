"""Telegram adapter: long polling, no public URL required.

The deadline monitor already knows how to *push* a notice to Telegram
(`jobs/notifier.py`). This module is the other direction: a person talks
to Ledger in a Telegram chat, and a document dropped into that chat is
ingested through the same local pipeline the web view uses.

Long polling is deliberate. A webhook would need a public HTTPS endpoint,
and publishing one would put the household database behind whatever
authentication the endpoint forgot to add. `getUpdates` works from a
laptop behind NAT with nothing exposed.

What leaves the machine:

* `getUpdates`, `getFile` and message sends go to `api.telegram.org`
  with the bot token;
* attachment downloads go to the file host Telegram returns;
* the model is never called by this adapter, so no notice text is sent
  to Token Factory here.

What comes back into the machine is untrusted text. It is handled by
`LedgerBot`, which treats it as data and never as instructions; the full
pipeline in `agent/` does the parsing locally.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from interface.bot.handlers import BotMessage, LedgerBot

__all__ = ["DEFAULT_API_URL", "TelegramAPI", "TelegramAuthError", "TelegramBot", "TelegramError"]

DEFAULT_API_URL = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096


class TelegramError(RuntimeError):
    """A Telegram API call failed.

    Messages carry the method name and Telegram's own description. They
    never carry the request URL, because the URL contains the bot token.
    """


class TelegramAuthError(TelegramError):
    """The bot token is rejected. Retrying cannot fix it."""


def _chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split a reply into Telegram-sized pieces, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


class TelegramAPI:
    """The small part of the Bot API this adapter needs.

    The client is injectable so tests never open a socket, and so a
    deployment can share one `httpx.Client` across adapters.
    """

    def __init__(
        self,
        token: str = "",
        *,
        base_url: str = DEFAULT_API_URL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required for the Telegram adapter")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def call(
        self,
        method: str,
        *,
        request_timeout: float | None = None,
        **params: Any,
    ) -> Any:
        """Call one Bot API method and return its `result`."""
        url = f"{self.base_url}/bot{self.token}/{method}"
        try:
            response = self._client.post(
                url, json=params, timeout=request_timeout or self.timeout
            )
        except httpx.HTTPError as exc:
            raise TelegramError(
                f"Telegram {method} transport failed: {type(exc).__name__}"
            ) from exc

        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(
                f"Telegram {method} returned a non-JSON response"
            ) from None

        if response.status_code != 200 or not payload.get("ok"):
            description = payload.get("description") or f"HTTP {response.status_code}"
            kind = (
                TelegramAuthError
                if response.status_code == 401 or payload.get("error_code") == 401
                else TelegramError
            )
            raise kind(f"Telegram {method} failed: {description}")
        return payload.get("result")

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        """Long-poll for messages.

        The HTTP timeout has to outlive the long poll, otherwise the client
        gives up just before Telegram would have answered.
        """
        result = self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message"],
            request_timeout=timeout + 10,
        )
        return list(result or [])

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Send a plain-text reply, split if it exceeds Telegram's limit."""
        for part in _chunks(text):
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": True,
            }
            if reply_to:
                params["reply_to_message_id"] = reply_to
            self.call("sendMessage", **params)

    def download(
        self,
        file_id: str,
        *,
        directory: Path | str | None = None,
        name: str = "",
    ) -> Path:
        """Download a document to a temporary file and return its path.

        Telegram's file path is only valid for a short time, so this calls
        `getFile` first and then fetches the returned path straight away.
        """
        info = self.call("getFile", file_id=file_id)
        remote = str((info or {}).get("file_path") or "")
        if not remote:
            raise TelegramError("Telegram getFile returned no file path")

        suffix = Path(name).suffix or Path(remote).suffix
        handle, temporary = tempfile.mkstemp(
            dir=directory, prefix="ledger-telegram-", suffix=suffix
        )
        destination = Path(temporary)
        url = f"{self.base_url}/file/bot{self.token}/{remote}"
        try:
            with (
                os.fdopen(handle, "wb") as output,
                self._client.stream("GET", url, timeout=self.timeout) as response,
            ):
                if response.status_code != 200:
                    raise TelegramError(
                        f"Telegram file download failed: HTTP {response.status_code}"
                    )
                for chunk in response.iter_bytes():
                    output.write(chunk)
        except httpx.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise TelegramError(
                f"Telegram file download failed: {type(exc).__name__}"
            ) from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination


class TelegramBot:
    """Turn Telegram updates into Ledger messages and replies.

    The adapter owns transport and temporary files; `LedgerBot` owns the
    conversation. That split is what keeps the bot testable without a
    network: the adapter can be driven with a fake Bot API.
    """

    def __init__(
        self,
        bot: LedgerBot,
        api: TelegramAPI,
        *,
        workdir: Path | str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.bot = bot
        self.api = api
        self.workdir = Path(workdir) if workdir else None
        self._log = log or (lambda message: print(message, file=sys.stderr))

    def handle_update(self, update: Mapping[str, Any]) -> str | None:
        """Handle one update, send the reply, and return the reply text.

        Returns `None` for updates that are not a chat message (for
        example a callback or an edited message that has no text).
        """
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None

        text = str(message.get("text") or message.get("caption") or "").strip()
        document = message.get("document")
        failures: list[str] = []

        if isinstance(document, Mapping):
            with tempfile.TemporaryDirectory(dir=self.workdir) as temporary:
                try:
                    path = self.api.download(
                        str(document.get("file_id", "")),
                        directory=Path(temporary),
                        name=str(document.get("file_name") or ""),
                    )
                except (TelegramError, KeyError, TypeError):
                    failures.append("I could not download that attachment.")
                else:
                    bot_message = BotMessage(
                        text=text,
                        chat_id=str(chat_id),
                        user_id=str((message.get("from") or {}).get("id") or "") or None,
                        message_id=str(message.get("message_id") or "") or None,
                        attachments=[str(path)],
                    )
                    reply = self.bot.on_attachment(bot_message)
                    self.api.send_message(str(chat_id), reply)
                    return reply

        if not text:
            if not failures:
                return None
            reply = "\n".join(failures)
            self.api.send_message(str(chat_id), reply)
            return reply

        reply = self.bot.on_message(
            BotMessage(
                text=text,
                chat_id=str(chat_id),
                user_id=str((message.get("from") or {}).get("id") or "") or None,
                message_id=str(message.get("message_id") or "") or None,
            )
        )
        if failures:
            reply = "\n".join([*failures, reply])
        self.api.send_message(str(chat_id), reply)
        return reply

    def poll_once(self, offset: int = 0, *, timeout: int = 25) -> int:
        """Fetch one batch and handle it. Returns the next update offset.

        A bad update is logged and skipped: one malformed message must
        not stop the poller, and personal content must not reach the log.
        """
        updates = self.api.get_updates(offset=offset or None, timeout=timeout)
        for update in updates:
            try:
                update_id = int(update["update_id"])
            except (KeyError, TypeError, ValueError):
                continue
            offset = max(offset, update_id + 1)
            try:
                self.handle_update(update)
            except Exception as exc:  # transport or platform failure
                self._log(f"telegram: update {update_id} failed ({type(exc).__name__})")
        return offset

    def run_forever(
        self,
        *,
        offset: int = 0,
        timeout: int = 25,
        stop: Callable[[], bool] | None = None,
        backoff: float = 2.0,
    ) -> None:
        """Poll until stopped or interrupted.

        A failed long poll waits `backoff` seconds rather than spinning.
        Telegram returns an empty list for a normal idle timeout, so the
        loop stays quiet when nothing is happening.
        """
        while not (stop and stop()):
            try:
                offset = self.poll_once(offset, timeout=timeout)
            except TelegramAuthError:
                raise
            except TelegramError as exc:
                self._log(str(exc))
                time.sleep(backoff)
            except KeyboardInterrupt:
                return


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    print(
        "Run the adapter with `python -m interface.chat`; "
        "this module has no standalone entrypoint.",
        file=sys.stderr,
    )
    raise SystemExit(2)
