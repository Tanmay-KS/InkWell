"""Tests for the chat interface.

The bot is the part of InkWell a person talks to, so its failures are
visible. These tests cover the two things that were wrong before Phase 4:
slash commands being swallowed by the natural-language keyword check, and
a status change that did not carry the effective date the user gave.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory
from interface.bot.handlers import BotMessage, InkWellBot

TODAY = date.today()


@pytest.fixture
def memory(tmp_path):
    with HouseholdMemory(tmp_path / "memory.db") as store:
        store.upsert(HouseholdItem(
            item_id=HouseholdItem.make_id("Orchard Fitness", "Taylor Reed", "GYM-1"),
            kind="membership",
            provider="Orchard Fitness",
            holder="Taylor Reed",
            reference="GYM-1",
            renewal_date=TODAY + timedelta(days=16),
        ))
        store.upsert(HouseholdItem(
            item_id=HouseholdItem.make_id("RiverFlix", "Taylor Reed", "SUB-2"),
            kind="subscription",
            provider="RiverFlix",
            holder="Taylor Reed",
            reference="SUB-2",
            renewal_date=TODAY + timedelta(days=7),
        ))
        store.upsert(HouseholdItem(
            item_id=HouseholdItem.make_id("Cedar Pool", "Taylor Reed", "POOL-3"),
            kind="membership",
            provider="Cedar Pool",
            holder="Taylor Reed",
            reference="POOL-3",
            renewal_date=TODAY + timedelta(days=20),
        ))
        yield store


def _bot(memory, **kwargs) -> InkWellBot:
    return InkWellBot(memory, **kwargs)


def _say(bot: InkWellBot, text: str) -> str:
    return bot.on_message(BotMessage(text=text, chat_id="test-chat", message_id="m-1"))


def test_help_lists_the_commands(memory):
    assert "/cancel" in _say(_bot(memory), "/help")


def test_due_lists_upcoming_items_but_not_cancelled_ones(memory):
    reply = _say(_bot(memory), "/due")
    assert "Orchard Fitness" in reply
    assert "RiverFlix" in reply

    # The id is derived; find it rather than hard-coding the hash.
    river = next(item for item in memory.all_items() if item.provider == "RiverFlix")
    memory.record_subscription_status(river.item_id, "cancelled")
    reply = _say(_bot(memory), "/due")
    assert "RiverFlix" not in reply


def test_list_shows_status(memory):
    reply = _say(_bot(memory), "Show my items")
    assert "Orchard Fitness" in reply
    assert "active" in reply


def test_a_named_item_answers_with_its_renewal_date(memory):
    reply = _say(_bot(memory), "When does Orchard Fitness renew?")
    orchard = next(item for item in memory.all_items() if item.provider == "Orchard Fitness")
    assert orchard.renewal_date.isoformat() in reply


def test_natural_language_cancel_records_the_change(memory):
    reply = _say(_bot(memory), "Cancel Orchard Fitness")
    orchard = next(item for item in memory.all_items() if item.provider == "Orchard Fitness")
    assert "cancelled" in reply
    assert orchard.item_id not in {item.item_id for item in memory.due_within(30)}


def test_slash_command_cancel_records_the_change(memory):
    """Regression: the generic keyword check used to answer this instead."""
    orchard = next(item for item in memory.all_items() if item.provider == "Orchard Fitness")
    reply = _say(_bot(memory), f"/cancel {orchard.item_id}")
    assert "Recorded" in reply
    assert "cancelled" in reply
    assert orchard.item_id not in {item.item_id for item in memory.due_within(30)}


def test_a_dated_status_change_uses_that_date(memory):
    orchard = next(item for item in memory.all_items() if item.provider == "Orchard Fitness")
    effective = (TODAY + timedelta(days=3)).isoformat()
    reply = _say(_bot(memory), f"/cancel {orchard.item_id} {effective}")
    assert effective in reply

    from agent.memory.update_tracker import FactKind, UpdateTracker

    when = date.fromisoformat(effective)
    current = UpdateTracker(memory).current(orchard.item_id, FactKind.SUBSCRIPTION_STATUS, when=when)
    assert current is not None
    assert current.valid_from.isoformat() == effective
    assert current.value == "cancelled"
    # It is still active today, so today's brief is unchanged.
    assert orchard.item_id in {item.item_id for item in memory.due_within(30)}


def test_reactivate_brings_the_item_back(memory):
    orchard = next(item for item in memory.all_items() if item.provider == "Orchard Fitness")
    _say(_bot(memory), f"/cancel {orchard.item_id}")
    reply = _say(_bot(memory), f"/active {orchard.item_id}")
    assert "active" in reply
    assert orchard.item_id in {item.item_id for item in memory.due_within(30)}


def test_an_ambiguous_request_changes_nothing(memory):
    reply = _say(_bot(memory), "Cancel the membership")
    assert "Which item" in reply
    for item in memory.all_items():
        assert item.item_id in {due.item_id for due in memory.due_within(30)}


def test_unknown_slash_command_is_explained(memory):
    reply = _say(_bot(memory), "/banish Orchard Fitness")
    assert "don't know that command" in reply


def test_unknown_text_does_not_invent_an_answer(memory):
    reply = _say(_bot(memory), "What is the airspeed velocity of an unladen swallow?")
    assert "I can help" in reply


def test_attachments_can_be_disabled(memory):
    bot = _bot(memory, allow_attachments=False)
    assert bot.on_attachment(BotMessage(text="", chat_id="c", attachments=["doc.pdf"])) == (
        "Document uploads are disabled."
    )


def test_an_unreadable_attachment_is_reported(memory):
    reply = _say(_bot(memory), "hello")
    assert "I can" in reply
    assert "could not be opened" in _bot(memory).on_attachment(
        BotMessage(text="", chat_id="c", attachments=["/nonexistent/notice.pdf"])
    )


def test_a_document_attachment_is_tracked_and_the_preview_is_redacted(memory, tmp_path):
    notice = tmp_path / "notice.txt"
    notice.write_text(
        "Meridian Mutual Insurance\nRenewal Notice\n\n"
        "Policy holder: Taylor Reed\n"
        "Your policy renews on 2027-03-07. Premium $148.95.\n"
        "Reference: POL-482913-Z\n",
        encoding="utf-8",
    )
    reply = _bot(memory).on_attachment(
        BotMessage(text="", chat_id="c", attachments=[str(notice)])
    )
    assert "Meridian Mutual" in reply
    assert "2027-03-07" in reply
    # The chat preview masks personal details; the full draft stays local.
    assert "Taylor Reed" not in reply
    assert any(item.provider == "Meridian Mutual Insurance" for item in memory.all_items())


def test_no_memory_is_an_explicit_error():
    assert InkWellBot().on_message(BotMessage(text="/due", chat_id="c")) == (
        "InkWell memory is not configured."
    )
