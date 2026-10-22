"""Tests for the Phase 1 pipeline.

The Phase 1 exit condition is: *feed a document in, get a flagged
deadline and a draft action out the other side.* These tests are that
condition, written down.

The privacy properties are tested alongside the functional ones, because
a pipeline that leaks is not a pipeline that works.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.actions.draft_actions import ActionKind, draft_action
from agent.deadlines.flag_deadlines import DeadlineKind, flag_deadlines, normalize_date
from agent.ingestion.ocr_text import repair_ocr_text
from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState
from agent.pipeline import (
    guess_amount,
    guess_kind,
    guess_provider,
    pick_deadline,
    process_document,
)

TODAY = date(2026, 10, 20)

INSURANCE_NOTICE = """Meridian Mutual Insurance
Renewal Notice

Dear Devin Sullivan,

Your homeowners policy is scheduled to renew on 2027-03-07. Your current premium is $148.95.

Policy number: POL-482913-Z
Account number: 1234-5678
Policy holder: Devin Sullivan
Insured address: 4128 Fenwick Street, Northgate, OR 97418
Contact: sam.rivera@example.com | (503) 555-0148

To renew, no action is required. To cancel instead, notify us in writing before 2027-03-07.

We value your continued business.
Meridian Mutual Insurance
"""

LICENCE_NOTICE = """OFFICE OF RECORD KEEPING
Official Notice of Expiry

This certifies that Casey Lindqvist holds a professional registration with the City Business Licensing Office, numbered ACC-427352.

Registered holder: Casey Lindqvist
Registration number: ACC-427352
Registered address: 1003 Braddock Boulevard, Cedar Crossing, TX 75204
Date of issue: 2026-02-26
Date of expiry: 2027-02-26

Correspondence to this office should be directed to c.lindqvist@example.com or 589-591-1209. A renewal fee of $266.64 per month applies.

Failure to renew will result in lapse of the registration.
"""


@pytest.fixture
def memory(tmp_path):
    store = HouseholdMemory(tmp_path / "memory.db")
    yield store
    store.close()


# --- dates ------------------------------------------------------------------


def test_normalizes_an_iso_date():
    assert normalize_date("2027-03-07") == date(2027, 3, 7)


def test_normalizes_a_written_date():
    assert normalize_date("15 January 2027") == date(2027, 1, 15)
    assert normalize_date("Jan 15, 2027") == date(2027, 1, 15)
    assert normalize_date("15 Jan 2027") == date(2027, 1, 15)


def test_an_ambiguous_numeric_date_is_resolved_towards_the_future():
    """01/02/2027 on 20 January 2027 means 1 February, not 2 January."""
    assert normalize_date("01/02/2027", today=date(2027, 1, 20)) == date(2027, 2, 1)


def test_an_unambiguous_numeric_date_is_read_correctly():
    assert normalize_date("15/01/2027") == date(2027, 1, 15)
    assert normalize_date("01/15/2027") == date(2027, 1, 15)


def test_relative_language():
    assert normalize_date("in 30 days", today=TODAY) == date(2026, 11, 19)
    assert normalize_date("within 2 weeks", today=TODAY) == date(2026, 11, 3)


def test_text_that_is_not_a_date_is_not_a_date():
    assert normalize_date("no obligation here") is None
    assert normalize_date("Policy 1234-5678") is None


# --- deadline flagging ------------------------------------------------------


def test_flags_a_renewal_with_evidence():
    deadlines = flag_deadlines(INSURANCE_NOTICE, today=TODAY)
    assert deadlines
    assert deadlines[0].kind is DeadlineKind.RENEWAL
    assert deadlines[0].date == date(2027, 3, 7)
    assert "2027-03-07" in deadlines[0].evidence


def test_a_date_mentioned_twice_is_one_deadline():
    """The notice says the date twice — "renews on" and "renew by"."""
    deadlines = flag_deadlines(INSURANCE_NOTICE, today=TODAY)
    assert len(deadlines) == 1


def test_the_date_of_issue_is_not_a_deadline():
    """It is when the document was written, not an obligation."""
    deadlines = flag_deadlines(LICENCE_NOTICE, today=TODAY)
    assert [d.date for d in deadlines] == [date(2027, 2, 26)]


def test_a_deadline_always_carries_its_evidence():
    for text in (INSURANCE_NOTICE, LICENCE_NOTICE):
        for deadline in flag_deadlines(text, today=TODAY):
            assert deadline.evidence, deadline


def test_a_past_deadline_is_flagged_but_not_hidden():
    deadlines = flag_deadlines(
        "Your policy expired on 2026-01-01. Failure to renew will result in lapse.",
        today=TODAY,
    )
    assert deadlines
    assert deadlines[0].is_past
    assert deadlines[0].date == date(2026, 1, 1)


def test_empty_text_has_no_deadlines():
    assert flag_deadlines("", today=TODAY) == []
    assert flag_deadlines("   \n  ", today=TODAY) == []


# --- extraction heuristics --------------------------------------------------


def test_guesses_the_provider_from_the_letterhead():
    assert guess_provider(INSURANCE_NOTICE) == "Meridian Mutual Insurance"


def test_guesses_the_provider_from_the_body_when_the_letterhead_is_a_heading():
    assert guess_provider(LICENCE_NOTICE) == "City Business Licensing Office"


def test_guesses_the_kind():
    assert guess_kind(INSURANCE_NOTICE) == "insurance"
    assert guess_kind(LICENCE_NOTICE) == "licence"


def test_guesses_the_amount():
    assert guess_amount(INSURANCE_NOTICE) == 148.95
    assert guess_amount("no money here") is None


# --- OCR repair -------------------------------------------------------------


def test_repairs_a_missing_space_after_a_colon():
    assert "Account number: 1234" in repair_ocr_text("Accountnumber:1234")


def test_does_not_break_a_thousands_separator():
    """`1,234.56` is a number, and a space in it is a broken number."""
    assert repair_ocr_text("The amount is 1,234.56 today.") == "The amount is 1,234.56 today."


def test_does_not_break_a_time():
    assert repair_ocr_text("at 12:30 today") == "at 12:30 today"


def test_splits_a_run_together_word():
    assert "1003 Braddock Boulevard" in repair_ocr_text("Registered address:1003BraddockBoulevard")


def test_does_not_split_an_identifier():
    """`AcC-427352` is an account number, not two words."""
    assert repair_ocr_text("Registration number:AcC-427352") == "Registration number: AcC-427352"


def test_repairs_a_run_together_label():
    assert "Date of expiry" in repair_ocr_text("Dateof expiry:2027-02-26")


def test_the_repair_is_a_no_op_on_clean_text():
    """A repair that changes clean text is a repair that damages it."""
    clean = INSURANCE_NOTICE
    assert repair_ocr_text(clean) == clean


# --- memory -----------------------------------------------------------------


def test_upsert_and_get(memory):
    item = HouseholdItem(
        item_id=HouseholdItem.make_id("Meridian Mutual Insurance", "Devin Sullivan", "1234-5678"),
        kind="insurance",
        provider="Meridian Mutual Insurance",
        holder="Devin Sullivan",
        reference="1234-5678",
        renewal_date=date(2027, 3, 7),
    )
    memory.upsert(item)
    fetched = memory.get(item.item_id)
    assert fetched is not None
    assert fetched.provider == "Meridian Mutual Insurance"
    assert fetched.renewal_date == date(2027, 3, 7)


def test_the_same_document_twice_is_one_item(memory):
    """Two copies of a renewal notice must not become two obligations."""
    first = HouseholdItem(
        item_id=HouseholdItem.make_id("Acme", "Sam", "1234"),
        kind="subscription",
        provider="Acme",
        holder="Sam",
    )
    memory.upsert(first)
    memory.upsert(first)
    assert len(memory.all_items()) == 1


def test_a_dismissed_item_stays_dismissed_when_it_is_seen_again(memory):
    """The user said "leave it alone" and the agent must not bring it back."""
    item = HouseholdItem(
        item_id=HouseholdItem.make_id("Acme", "Sam", "1234"),
        kind="subscription",
        provider="Acme",
        holder="Sam",
    )
    memory.upsert(item)
    memory.set_state(item.item_id, TrackedState.DISMISSED)

    memory.upsert(item)
    assert memory.get(item.item_id).state is TrackedState.DISMISSED


def test_due_within_finds_the_soon_and_dismisses_the_rest(memory):
    soon = HouseholdItem(
        item_id=HouseholdItem.make_id("Acme", "Sam", "1"),
        kind="subscription",
        provider="Acme",
        renewal_date=date(2026, 11, 1),
    )
    later = HouseholdItem(
        item_id=HouseholdItem.make_id("Beta", "Sam", "2"),
        kind="subscription",
        provider="Beta",
        renewal_date=date(2027, 6, 1),
    )
    memory.upsert(soon)
    memory.upsert(later)
    memory.set_state(later.item_id, TrackedState.DISMISSED)

    due = memory.due_within(30, today=TODAY)
    assert [item.provider for item in due] == ["Acme"]


def test_the_handled_log_stops_the_agent_repeating_itself(memory):
    item = HouseholdItem(
        item_id=HouseholdItem.make_id("Acme", "Sam", "1"),
        kind="subscription",
        provider="Acme",
    )
    memory.upsert(item)
    assert not memory.was_handled(item.item_id, "notified")
    memory.record_event(item.item_id, "notified", "said it once")
    assert memory.was_handled(item.item_id, "notified")
    assert memory.was_handled(item.item_id, "notified")


def test_the_household_names_come_from_the_memory(memory):
    """This is the connection between the memory layer and the redactor."""
    memory.upsert(
        HouseholdItem(
            item_id=HouseholdItem.make_id("Acme", "Devin Sullivan", "1"),
            kind="insurance",
            provider="Acme",
            holder="Devin Sullivan",
        )
    )
    assert memory.household_names() == ["Devin Sullivan"]


def test_the_household_summary_is_written_for_the_agent(memory):
    memory.upsert(
        HouseholdItem(
            item_id=HouseholdItem.make_id("Acme", "Devin Sullivan", "1"),
            kind="insurance",
            provider="Acme",
            holder="Devin Sullivan",
            renewal_date=date(2027, 3, 7),
        )
    )
    summary = memory.household_summary()
    assert "Devin Sullivan" in summary
    assert "Acme" in summary
    assert "2027-03-07" in summary


# --- drafting ----------------------------------------------------------------


def test_drafts_a_cancellation_with_the_real_details(memory):
    deadline = flag_deadlines(INSURANCE_NOTICE, today=TODAY)[0]
    item = HouseholdItem(
        item_id=HouseholdItem.make_id("Meridian Mutual Insurance", "Devin Sullivan", "1234-5678"),
        kind="insurance",
        provider="Meridian Mutual Insurance",
        holder="Devin Sullivan",
        reference="1234-5678",
        renewal_date=deadline.date,
    )
    action = draft_action(deadline, context=item, kind=ActionKind.CANCEL)

    assert action.kind is ActionKind.CANCEL
    assert "Meridian Mutual Insurance" in action.body
    assert "1234-5678" in action.body
    assert "Devin Sullivan" in action.body
    assert "7 March 2027" in action.body
    assert "1234-5678" in action.personal_details


def test_a_draft_has_no_placeholders_and_no_meta_commentary():
    """The body has to be sendable as-is by a human."""
    deadline = flag_deadlines(INSURANCE_NOTICE, today=TODAY)[0]
    action = draft_action(deadline)
    assert "[YOUR_NAME]" not in action.body
    assert "as an AI" not in action.body
    assert "AI assistant" not in action.body


def test_the_redacted_draft_has_no_personal_details(memory):
    """The version that leaves the sandbox is the redacted one."""
    deadline = flag_deadlines(INSURANCE_NOTICE, today=TODAY)[0]
    item = HouseholdItem(
        item_id=HouseholdItem.make_id("Meridian Mutual Insurance", "Devin Sullivan", "1234-5678"),
        kind="insurance",
        provider="Meridian Mutual Insurance",
        holder="Devin Sullivan",
        reference="1234-5678",
    )
    action = draft_action(deadline, context=item, kind=ActionKind.CANCEL)
    redacted = action.redacted_body()
    assert "1234-5678" not in redacted
    assert "Devin Sullivan" not in redacted


# --- the pipeline ------------------------------------------------------------


def test_the_pipeline_produces_a_deadline_and_a_draft(tmp_path):
    """The Phase 1 exit condition."""
    source = tmp_path / "notice.txt"
    source.write_text(INSURANCE_NOTICE, encoding="utf-8")

    result = process_document(source, today=TODAY)

    assert result.ok
    assert result.deadlines
    assert result.deadlines[0].date == date(2027, 3, 7)
    assert result.action is not None
    assert result.item is not None
    assert result.item.provider == "Meridian Mutual Insurance"


def test_the_pipeline_never_puts_a_personal_detail_in_the_document_text(tmp_path):
    """The document the pipeline holds is the redacted one."""
    source = tmp_path / "notice.txt"
    source.write_text(INSURANCE_NOTICE, encoding="utf-8")

    result = process_document(source, today=TODAY)

    assert "Devin Sullivan" not in result.document.text
    assert "1234-5678" not in result.document.text
    assert "sam.rivera@example.com" not in result.document.text
    # ...but the real values are available inside the sandbox.
    assert "Devin Sullivan" in result.document.redactions.names()


def test_the_pipeline_stores_the_item_in_the_memory(tmp_path, memory):
    source = tmp_path / "notice.txt"
    source.write_text(INSURANCE_NOTICE, encoding="utf-8")

    result = process_document(source, memory=memory, today=TODAY)

    stored = memory.get(result.item.item_id)
    assert stored is not None
    assert stored.reference == "POL-482913-Z", "the policy number, not the account number"
    assert stored.holder == "Devin Sullivan"


def test_the_pipeline_reports_a_document_it_cannot_read(tmp_path):
    result = process_document(tmp_path / "missing.pdf", today=TODAY)
    assert not result.ok
    assert result.warnings


def test_the_document_serialisable_form_carries_no_personal_details(tmp_path):
    source = tmp_path / "notice.txt"
    source.write_text(INSURANCE_NOTICE, encoding="utf-8")
    result = process_document(source, today=TODAY)

    import json

    serialised = json.dumps(result.document.serialisable())
    assert "1234-5678" not in serialised
    assert "Devin Sullivan" not in serialised


def test_picking_the_deadline_prefers_a_future_one():
    past = flag_deadlines("Your policy expired on 2026-01-01.", today=TODAY)[0]
    future = flag_deadlines("Your policy renews on 2027-03-07.", today=TODAY)[0]
    assert pick_deadline([past, future], today=TODAY) is future
