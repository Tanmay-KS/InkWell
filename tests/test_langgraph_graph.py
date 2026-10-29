"""Tests for the LangGraph role graph.

The graph is the framework implementation of the same contract the plain
orchestrator exposes. These tests pin the properties that matter:

* the compiled graph runs the roles in order and the trace is complete;
* a planted error goes Auditor -> Drafter -> Auditor and is approved;
* the retry cap stops at `needs_human_review`;
* the optional model Drafter records the model, tokens and egress;
* a missing key or pinned model raises instead of falling back.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.actions.draft_actions import ActionKind, draft_action
from agent.graph import MAX_REVISIONS, run_text_graph
from agent.model import ModelReply, NebiusClient, pinned_model

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

To cancel instead, notify us in writing before 2027-03-07.
"""


class FakeClient:
    """A deterministic stand-in for the Token Factory client."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[tuple[str, str]] = []

    def complete(self, role: str, *, system: str, user: str, **_: object) -> ModelReply:
        self.calls.append((role, user))
        return ModelReply(
            text=self.body,
            model="nvidia/nemotron-test-super",
            tier=role,
            latency_ms=7,
            tokens_in=321,
            tokens_out=123,
        )


MODEL_BODY = """Dear Meridian Mutual Insurance,

I am writing to request a refund of 148.95 for my homeowners policy.

Policy number: POL-482913-Z

The renewal date on my notice is 07 March 2027. Please confirm the refund and tell me what you need from me.

Kind regards,
Taylor Reed"""


def test_the_graph_runs_every_role_and_approves():
    state = run_text_graph(NOTICE, today=TODAY, action_kind=ActionKind.REFUND)

    assert state.status == "ok"
    assert state.retries == 0
    assert state.audit_result is not None
    assert state.audit_result.approved

    agents = [event.agent for event in state.trace.events]
    assert agents[:5] == ["Redactor", "Extractor", "Verifier", "Researcher", "Drafter"]
    assert agents.count("Auditor") == 1
    assert agents[-1] == "Human approval"
    assert all(event.egress == "none" for event in state.trace.events)
    assert state.trace.summary()["model_calls"] == 0


def test_a_planted_error_takes_the_conditional_edge_back_to_the_drafter():
    def plant(deadline, item, facts):
        draft = draft_action(deadline, context=item, kind=ActionKind.REFUND)
        draft.body = draft.body.replace("148.95", "184.95")
        return draft

    state = run_text_graph(
        NOTICE, today=TODAY, action_kind=ActionKind.REFUND, drafter=plant
    )

    assert state.status == "ok"
    assert state.retries == 1
    statuses = [
        event.status for event in state.trace.events if event.agent == "Auditor"
    ]
    assert statuses == ["rejected", "ok"]
    assert state.audit_result is not None and state.audit_result.approved
    assert state.draft is not None and "148.95" in state.draft.body


def test_the_retry_cap_stops_at_needs_human_review():
    def empty(deadline, item, facts):
        draft = draft_action(deadline, context=item, kind=ActionKind.REFUND)
        draft.body = ""
        return draft

    state = run_text_graph(
        NOTICE, today=TODAY, action_kind=ActionKind.REFUND, drafter=empty
    )

    assert state.status == "needs_human_review"
    assert state.retries == MAX_REVISIONS
    auditor_events = [
        event for event in state.trace.events if event.agent == "Auditor"
    ]
    assert len(auditor_events) == MAX_REVISIONS + 1
    assert all(event.status == "rejected" for event in auditor_events)


def test_the_model_drafter_records_model_tokens_and_egress():
    client = FakeClient(MODEL_BODY)

    state = run_text_graph(
        NOTICE,
        today=TODAY,
        action_kind=ActionKind.REFUND,
        use_model_drafter=True,
        model_client=client,  # type: ignore[arg-type]
    )

    assert client.calls and client.calls[0][0] == "super"
    assert state.status == "ok"
    drafter = next(event for event in state.trace.events if event.agent == "Drafter")
    assert drafter.model == "nvidia/nemotron-test-super"
    assert drafter.tier == "super"
    assert drafter.tokens_in == 321
    assert drafter.tokens_out == 123
    assert drafter.egress == "allowed:api.tokenfactory.nebius.com"
    assert state.trace.summary()["model_calls"] == 1


def test_a_missing_model_client_is_an_error_not_a_template_fallback():
    state = run_text_graph(
        NOTICE, today=TODAY, action_kind=ActionKind.REFUND, use_model_drafter=True
    )

    assert state.status == "error"
    assert any("without a Token Factory client" in warning for warning in state.warnings)


def test_no_api_key_raises_instead_of_calling_another_provider(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY"):
        NebiusClient().complete("super", system="s", user="u")


def test_a_missing_pinned_model_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEDGER_MODEL_SUPER", raising=False)

    with pytest.raises(RuntimeError, match="LEDGER_MODEL_SUPER"):
        pinned_model("super")
