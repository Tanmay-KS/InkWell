"""Tests for the Telegram adapter.

No network is touched: the Bot API is a fake object, and the long-poll
loop is driven one batch at a time. The properties that matter are the
ones that would break a live bot — replying to the right chat, ingesting
an attachment exactly once, advancing the offset, and surviving one bad
update without losing the rest of the batch.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory
from interface.bot.handlers import LedgerBot
from interface.chat.telegram import (
    TelegramAPI,
    TelegramAuthError,
    TelegramBot,
    TelegramError,
    _chunks,
)

TODAY = date.today()


@pytest.fixture
def memory(tmp_path):
    with HouseholdMemory(tmp_path / "memory.db") as store:
        store.upsert(
            HouseholdItem(
                item_id=HouseholdItem.make_id("Orchard Fitness", "Taylor Reed", "GYM-1"),
                kind="membership",
                provider="Orchard Fitness",
                holder="Taylor Reed",
                reference="GYM-1",
                renewal_date=TODAY + timedelta(days=16),
            )
        )
        yield store


def _notice(days: int = 90) -> str:
    renewal = (TODAY + timedelta(days=days)).isoformat()
    return f"""Meridian Mutual Insurance
Renewal Notice

Dear Taylor Reed,

Your homeowners policy is scheduled to renew on {renewal}.
Total: $148.95

Policy number: POL-482913-Z
"""


class FakeTelegramAPI:
    """Records sends and writes a notice instead of downloading bytes."""

    def __init__(self, updates: list[dict] | None = None, *, fails: set[int] | None = None):
        self.updates = updates or []
        self.fails = fails or set()
        self.sent: list[tuple[str, str]] = []
        self.requested_offsets: list[int | None] = []
        self.downloads: list[str] = []

    def get_updates(self, *, offset: int | None, timeout: int):
        self.requested_offsets.append(offset)
        return self.updates

    def send_message(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        self.sent.append((chat_id, text))

    def download(self, file_id: str, *, directory=None, name: str = "") -> Path:
        if file_id in self.fails:
            raise TelegramError("Telegram file download failed: HTTP 404")
        self.downloads.append(file_id)
        path = Path(directory or ".") / "notice.txt"
        path.write_text(_notice(), encoding="utf-8")
        return path


def _update(text: str = "", *, update_id: int = 1, chat_id: int = 42, **message) -> dict:
    payload = {
        "message_id": update_id,
        "chat": {"id": chat_id},
        "from": {"id": 7},
        "text": text,
    }
    payload.update(message)
    return {"update_id": update_id, "message": payload}


def test_a_text_message_is_answered_in_the_same_chat(memory):
    api = FakeTelegramAPI([_update("/due")])
    adapter = TelegramBot(LedgerBot(memory), api)  # type: ignore[arg-type]

    assert adapter.poll_once() == 2

    assert api.sent
    chat_id, reply = api.sent[0]
    assert chat_id == "42"
    assert "Orchard Fitness" in reply


def test_a_document_is_downloaded_and_ingested(memory):
    api = FakeTelegramAPI(
        [_update(document={"file_id": "file-1", "file_name": "notice.txt"})]
    )
    adapter = TelegramBot(LedgerBot(memory), api)  # type: ignore[arg-type]

    reply = adapter.handle_update(api.updates[0])

    assert api.downloads == ["file-1"]
    assert reply is not None and reply.startswith("Tracked Meridian Mutual Insurance")
    assert api.sent[0][1] == reply
    assert any(item.provider == "Meridian Mutual Insurance" for item in memory.all_items())


def test_a_failed_download_is_reported_without_stopping_the_poll(memory):
    api = FakeTelegramAPI(
        [
            _update(document={"file_id": "bad"}, update_id=5),
            _update("help", update_id=6),
        ],
        fails={"bad"},
    )
    adapter = TelegramBot(LedgerBot(memory), api)  # type: ignore[arg-type]

    offset = adapter.poll_once()

    assert offset == 7
    assert len(api.sent) == 2
    assert "could not download" in api.sent[0][1].casefold()
    assert "/cancel" in api.sent[1][1]


def test_updates_without_a_chat_are_ignored(memory):
    api = FakeTelegramAPI([{"update_id": 3, "callback_query": {"id": "x"}}])
    adapter = TelegramBot(LedgerBot(memory), api)  # type: ignore[arg-type]

    assert adapter.poll_once() == 4
    assert api.sent == []


def test_the_offset_is_not_sent_when_it_is_zero(memory):
    api = FakeTelegramAPI([])
    adapter = TelegramBot(LedgerBot(memory), api)  # type: ignore[arg-type]

    adapter.poll_once(0)

    assert api.requested_offsets == [None]


def test_long_replies_are_split_under_the_telegram_limit():
    text = "\n".join(f"line {index} " + "x" * 60 for index in range(120))

    parts = _chunks(text, limit=400)

    assert len(parts) > 1
    assert all(len(part) <= 400 for part in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_a_missing_token_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        TelegramAPI()


class _UnauthorizedResponse:
    status_code = 401

    def json(self) -> dict:
        return {"ok": False, "error_code": 401, "description": "Unauthorized"}


class _UnauthorizedClient:
    def post(self, *args, **kwargs) -> _UnauthorizedResponse:
        return _UnauthorizedResponse()


def test_a_rejected_token_is_a_fatal_error():
    api = TelegramAPI("bad-token", client=_UnauthorizedClient())  # type: ignore[arg-type]

    with pytest.raises(TelegramAuthError, match="Unauthorized"):
        api.get_updates(offset=None, timeout=0)


def test_the_poller_does_not_retry_a_rejected_token(memory):
    api = FakeTelegramAPI()

    def rejected(*, offset, timeout):
        raise TelegramAuthError("Telegram getUpdates failed: Unauthorized")

    api.get_updates = rejected  # type: ignore[method-assign]
    adapter = TelegramBot(LedgerBot(memory), api)  # type: ignore[arg-type]

    with pytest.raises(TelegramAuthError):
        adapter.run_forever(backoff=0)
