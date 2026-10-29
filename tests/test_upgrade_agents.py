"""Tests for the glass-box upgrade: trace, Verifier, Auditor, orchestrator.

The properties under test are the ones the video and the README claim:

* a run produces the documented trace schema with 5+ steps;
* the trace never stores raw personal detail;
* the Verifier computes totals, duplicates and days;
* the Auditor rejects a planted error, the revision repairs it, and the
  second pass is approved;
* the retry cap stops at `needs_human_review` instead of looping.
"""

from __future__ import annotations

import json
from datetime import date

from agent.actions.draft_actions import ActionKind, draft_action
from agent.auditor import AuditDecision, audit_draft, revise_draft
from agent.orchestrator import MAX_REVISIONS, run_text
from agent.trace import TraceLog, egress_label, safe_summary
from agent.verifier import (
    days_remaining,
    find_duplicate_charges,
    inventory_findings,
    verify,
)

TODAY = date(2026, 10, 20)

NOTICE = """Meridian Mutual Insurance
Renewal Notice

Dear Taylor Reed,

Your homeowners policy is scheduled to renew on 2027-03-07.

Premium: $120.00
Service fee: $28.95
Total: $148.95

Policy number: POL-482913-Z
Policy holder: Taylor Reed
Contact: taylor.reed@example.com | (503) 555-0148

To cancel instead, notify us in writing before 2027-03-07.
"""


# --- trace -----------------------------------------------------------------


def test_a_summary_is_redacted_before_it_is_stored():
    summary = safe_summary("Write to taylor.reed@example.com or (503) 555-0148", limit=200)
    assert "taylor.reed@example.com" not in summary
    assert "555-0148" not in summary
    assert "[EMAIL_" in summary


def test_every_event_carries_the_documented_schema():
    log = TraceLog(run_id="run-test")
    event = log.record(
        "Redactor",
        tier="local",
        input_summary="a document",
        output_summary="2 details masked",
    )

    assert event.step == 1
    assert set(event.to_dict()) == {
        "run_id",
        "step",
        "agent",
        "model",
        "tier",
        "latency_ms",
        "tokens_in",
        "tokens_out",
        "input_summary",
        "output_summary",
        "egress",
        "status",
    }
    assert egress_label("api.tavily.com") == "allowed:api.tavily.com"
    assert egress_label("api.tavily.com", blocked=True) == "blocked:api.tavily.com"
    assert egress_label(None) == "none"


def test_the_jsonl_round_trips(tmp_path):
    path = tmp_path / "trace.jsonl"
    log = TraceLog(run_id="run-jsonl", path=path)
    log.record("Verifier", tier="code", output_summary="totals match")
    log.record("Auditor", tier="code", status="rejected", output_summary="amount")

    loaded = TraceLog().read(path)

    assert [event.agent for event in loaded] == ["Verifier", "Auditor"]
    assert loaded[1].status == "rejected"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[1])["step"] == 2


# --- verifier --------------------------------------------------------------


def test_line_items_that_match_the_total():
    facts = verify(
        {
            "vendor": "Northstar Auto",
            "line_items": [
                {"label": "Premium", "amount": 120.00},
                {"label": "Service fee", "amount": 28.95},
            ],
            "stated_total": 148.95,
            "deadline": "2027-03-07",
        },
        today=TODAY,
    )

    assert facts.computed_total == 148.95
    assert facts.totals_match is True
    assert facts.days_remaining == 138
    assert "matching the stated total" in " ".join(facts.facts)


def test_line_items_that_do_not_match_the_total():
    facts = verify(
        {
            "line_items": [{"label": "Premium", "amount": 120.00}],
            "stated_total": 148.95,
        },
        today=TODAY,
    )

    assert facts.totals_match is False
    assert any("does not match" in fact for fact in facts.facts)


def test_duplicate_charges_need_the_same_vendor_amount_and_nearby_dates():
    duplicates = find_duplicate_charges(
        [
            {"provider": "Northstar Auto", "amount": 148.95, "date": "2027-03-01"},
            {"provider": "Northstar Auto", "amount": 148.95, "date": "2027-03-03"},
            {"provider": "Northstar Auto", "amount": 148.95, "date": "2027-06-01"},
            {"provider": "Orchard Fitness", "amount": 148.95, "date": "2027-03-02"},
        ]
    )

    assert len(duplicates) == 1
    assert duplicates[0]["days_apart"] == 2


def test_days_and_the_expiry_window():
    assert days_remaining("2026-10-25", today=TODAY) == 5
    assert days_remaining("2026-10-01", today=TODAY) == -19

    facts = verify({"deadline": "2026-10-25"}, today=TODAY)
    assert facts.expiring_soon is True
    assert "within 14 days" in " ".join(facts.facts)


def test_inventory_findings_summarises_deadlines_and_duplicates():
    from agent.memory.hermes_memory import HouseholdItem

    items = [
        HouseholdItem(
            item_id="a", kind="insurance", provider="Northstar Auto",
            renewal_date=date(2026, 10, 24), amount=148.95,
        ),
        HouseholdItem(
            item_id="b", kind="insurance", provider="Northstar Auto",
            renewal_date=date(2026, 10, 26), amount=148.95,
        ),
    ]

    findings = inventory_findings(items, today=TODAY)

    assert findings["summary"]
    assert {finding["kind"] for finding in findings["findings"]} == {
        "deadline",
        "duplicate_charge",
    }


# --- auditor ---------------------------------------------------------------


def _draft(kind: ActionKind = ActionKind.REFUND):
    from agent.deadlines.flag_deadlines import Deadline, DeadlineKind

    deadline = Deadline(
        kind=DeadlineKind.RENEWAL,
        title="Renewal",
        date=date(2027, 3, 7),
        evidence="Your homeowners policy is scheduled to renew on 2027-03-07.",
    )
    from agent.memory.hermes_memory import HouseholdItem

    item = HouseholdItem(
        item_id="item-test",
        kind="insurance",
        provider="Meridian Mutual Insurance",
        holder="Taylor Reed",
        reference="POL-482913-Z",
        renewal_date=date(2027, 3, 7),
        amount=148.95,
    )
    return draft_action(deadline, context=item, kind=kind), item


def test_the_auditor_catches_a_wrong_amount_and_revision_repairs_it():
    draft, item = _draft()
    facts = verify(
        {
            "vendor": item.provider,
            "amount": item.amount,
            "stated_total": item.amount,
            "deadline": item.renewal_date.isoformat(),
        },
        today=TODAY,
    )
    draft.body = draft.body.replace("148.95", "184.95")

    audit = audit_draft(draft, facts=facts, item=item)
    assert audit.decision is AuditDecision.REJECT
    assert "amount_mismatch" in audit.codes

    revised = revise_draft(draft, audit, facts=facts, item=item)
    second = audit_draft(revised, facts=facts, item=item)
    assert second.decision is AuditDecision.APPROVE


def test_the_auditor_catches_an_uncited_claim():
    draft, item = _draft()
    facts = verify(
        {"vendor": item.provider, "amount": item.amount, "deadline": "2027-03-07"},
        today=TODAY,
    )
    draft.body += " Your policy includes a 30-day cooling-off period."

    audit = audit_draft(draft, facts=facts, item=item)

    assert audit.decision is AuditDecision.REJECT
    assert "uncited_claim" in audit.codes


def test_the_auditor_catches_a_promised_outcome():
    draft, item = _draft()
    facts = verify(
        {"vendor": item.provider, "amount": item.amount, "deadline": "2027-03-07"},
        today=TODAY,
    )
    draft.body += " You will receive a refund within 30 days."

    audit = audit_draft(draft, facts=facts, item=item)

    assert audit.decision is AuditDecision.REJECT
    assert "promised_outcome" in audit.codes


# --- orchestrator ----------------------------------------------------------


def test_a_run_shows_the_roles_and_approves():
    state = run_text(NOTICE, today=TODAY, action_kind=ActionKind.REFUND)

    assert state.status == "ok"
    assert state.retries == 0
    assert state.audit_result is not None
    assert state.audit_result.decision is AuditDecision.APPROVE

    agents = [event.agent for event in state.trace.events]
    assert agents[:5] == ["Redactor", "Extractor", "Verifier", "Researcher", "Drafter"]
    assert "Auditor" in agents
    assert len(state.trace.events) >= 6
    assert all(event.egress == "none" for event in state.trace.events)
    assert state.trace.summary()["model_calls"] == 0


def test_the_retry_cap_stops_at_needs_human_review():
    def empty_drafter(deadline, item, facts):
        draft = draft_action(deadline, context=item, kind=ActionKind.REFUND)
        draft.body = ""
        return draft

    state = run_text(
        NOTICE,
        today=TODAY,
        action_kind=ActionKind.REFUND,
        drafter=empty_drafter,
    )

    assert state.status == "needs_human_review"
    assert state.retries == MAX_REVISIONS
    auditor_events = [event for event in state.trace.events if event.agent == "Auditor"]
    assert len(auditor_events) == MAX_REVISIONS + 1
    assert all(event.status == "rejected" for event in auditor_events)


def test_the_user_still_approves_the_outbound_action():
    state = run_text(NOTICE, today=TODAY, action_kind=ActionKind.REFUND)

    assert state.user_approved is False
    state.approve()
    assert state.user_approved is True
