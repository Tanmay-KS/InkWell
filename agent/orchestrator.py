"""Orchestrator: plain-Python roles with the plan's state and edges.

Upgrade Phase B — the documented fallback.

The primary role graph is `agent/graph.py` (LangGraph). This module keeps
the same roles, the same shared state and the same conditional Auditor
edge in plain Python, so the pipeline still runs if the framework is
unavailable and so the tests have a second, independent implementation to
compare against:

    Redactor -> Extractor -> Verifier -> Researcher -> Drafter -> Auditor
                                                  ^                |
                                                  +--- reject ------+
                                                       (max 2)

There is no LangGraph or CrewAI import anywhere in this file. That is the
point of a fallback: it must not depend on the thing it is falling back
from.

The state object is the one the plan names:

    { doc_id, redacted_text, extracted_fields, verified_facts,
      research_notes, draft, audit_result, retries, status }

Every transition is written to `agent.trace.TraceLog`. The redactor runs
first and the text that reaches the Extractor, the Drafter and the
Auditor is the redacted text; the real values stay in the document's
redaction map, inside the sandbox.

The user still approves every outbound action. `RunState.status` stops at
`ok` (approved draft ready for a human) or `needs_human_review`; there is
no send path in this module.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from agent.actions.draft_actions import ActionKind, DraftAction, draft_action
from agent.auditor import AuditResult, audit_draft, revise_draft
from agent.deadlines.flag_deadlines import Deadline, flag_deadlines
from agent.ingestion.parse_documents import Document, parse_document
from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory
from agent.pipeline import build_item, guess_amount, guess_provider, pick_deadline, redact_source
from agent.trace import TraceLog, egress_label
from agent.verifier import VerifiedFacts, verify
from privacy_router.egress import Transport
from privacy_router.redact import local_redact

__all__ = [
    "MAX_REVISIONS",
    "RunState",
    "extract_fields",
    "run_document",
    "run_text",
]

# The plan's hard cap: two rejections, then a human decides.
MAX_REVISIONS = 2

# A line item is a labelled amount on its own line. The label is kept in
# the summary only; it never leaves the sandbox.
_LINE_ITEM = re.compile(
    r"^\s*(?P<label>[A-Za-z][A-Za-z0-9 &/.,'\-]{2,60}?)"
    r"\s*[:=\-]?\s*\$?\s*(?P<amount>\d[\d,]*(?:\.\d{2}))\s*[.,]?\s*$",
    re.MULTILINE,
)

# A "total" line is the stated total, not another line item. Summing it
# with the items it is the sum of is the classic double-count.
_TOTAL_LABELS = {"total", "grand total", "amount due", "balance", "subtotal"}

Drafter = Callable[[Deadline, HouseholdItem | None, VerifiedFacts], DraftAction]
Researcher = Callable[[str], list[dict[str, object]]]


@dataclass(slots=True)
class RunState:
    """The shared state passed between roles."""

    doc_id: str
    path: str
    redacted_text: str = ""
    extracted_fields: dict[str, object] = field(default_factory=dict)
    verified_facts: VerifiedFacts | None = None
    research_notes: list[dict[str, object]] = field(default_factory=list)
    draft: DraftAction | None = None
    audit_result: AuditResult | None = None
    retries: int = 0
    status: str = "pending"
    trace: TraceLog = field(default_factory=TraceLog)
    document: Document | None = None
    item: HouseholdItem | None = None
    deadlines: list[Deadline] = field(default_factory=list)
    audit_history: list[AuditResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    user_approved: bool = False

    @property
    def ok(self) -> bool:
        """Whether the run produced an approved draft for a human."""
        return self.status == "ok" and self.draft is not None

    @property
    def needs_human_review(self) -> bool:
        return self.status == "needs_human_review"

    def approve(self) -> None:
        """Record the human's approval. Nothing is sent by this call."""
        self.user_approved = True

    def to_dict(self) -> dict[str, object]:
        """The state as the web view and tests consume it."""
        return {
            "doc_id": self.doc_id,
            "status": self.status,
            "retries": self.retries,
            "redacted_text": self.redacted_text,
            "extracted_fields": self.extracted_fields,
            "verified_facts": (
                self.verified_facts.to_dict() if self.verified_facts else None
            ),
            "research_notes": self.research_notes,
            "draft": (
                {
                    "title": self.draft.title,
                    "body": self.draft.redacted_body(),
                    "kind": self.draft.kind.value,
                    "recipient": self.draft.recipient,
                }
                if self.draft
                else None
            ),
            "audit": self.audit_result.to_dict() if self.audit_result else None,
            "audit_history": [result.to_dict() for result in self.audit_history],
            "warnings": self.warnings,
            "user_approved": self.user_approved,
            "trace": self.trace.to_dicts(),
            "trace_summary": self.trace.summary(),
        }


def extract_fields(
    document: Document,
    deadline: Deadline | None,
    *,
    item: HouseholdItem | None = None,
) -> dict[str, object]:
    """Turn the redacted document into the fields the Verifier checks.

    The amounts come from the redacted text (money is not personal
    detail); the vendor and reference come from the item, which was built
    while the redaction map was still in scope.
    """
    line_items = []
    stated_total: float | None = None
    for match in _LINE_ITEM.finditer(document.text):
        label = match.group("label").strip(" :-=")
        value = float(match.group("amount").replace(",", ""))
        if label.strip().lower() in _TOTAL_LABELS:
            stated_total = value
        else:
            line_items.append({"label": label, "amount": value})

    amount = item.amount if item is not None and item.amount is not None else guess_amount(document.text)
    return {
        "vendor": (item.provider if item else guess_provider(document.text)),
        "amount": amount,
        "stated_total": stated_total if stated_total is not None else amount,
        "line_items": line_items,
        "deadline": deadline.date.isoformat() if deadline else None,
        "deadline_kind": deadline.kind.value if deadline else None,
        "evidence": deadline.evidence if deadline else "",
        "redaction_count": len(document.redactions.values),
    }


def run_document(
    path: str | Path,
    *,
    memory: HouseholdMemory | None = None,
    today: date | None = None,
    kind: str = "",
    action_kind: ActionKind | None = None,
    redact: bool = True,
    transport: Transport | None = None,
    trace_path: str | Path | None = None,
    drafter: Drafter | None = None,
    researcher: Researcher | None = None,
    max_revisions: int = MAX_REVISIONS,
) -> RunState:
    """Run the full role graph over one document.

    Args:
        path: The document to read.
        memory: Household memory, if the item should be stored.
        today: Reference date.
        kind: Force the obligation kind.
        action_kind: Force the action kind.
        redact: The leak test's router-OFF condition runs with this off.
        transport: Optional vision fallback transport.
        trace_path: Write the JSONL trace here as well as keeping it in
            memory.
        drafter: Override the Drafter. Used by the planted-error demo.
        researcher: Optional research callable. It receives a redacted
            query and returns notes. No callable means no egress.
        max_revisions: The plan's retry cap. Two by default.
    """
    today = today or date.today()
    state = RunState(
        doc_id=uuid.uuid4().hex[:12],
        path=str(path),
        trace=TraceLog(path=Path(trace_path) if trace_path else None),
    )

    names: list[str] = []
    if memory is not None:
        try:
            names = memory.household_names()
        except Exception as exc:  # pragma: no cover - defensive
            state.warnings.append(f"could not read household names ({type(exc).__name__})")

    # --- Redactor ---------------------------------------------------------
    started = time.perf_counter()
    try:
        document = parse_document(
            path,
            model=None,  # the local extractor is the primary path
            transport=transport,
            household_names=names,
            redact=redact,
        )
    except Exception as exc:
        state.trace.timed(
            "Redactor",
            started=started,
            input_summary=redact_source(path, names),
            output_summary=f"failed: {type(exc).__name__}",
            status="error",
        )
        state.status = "error"
        state.warnings.append(f"extraction failed: {type(exc).__name__}")
        return state

    state.document = document
    state.redacted_text = document.text
    state.warnings.extend(document.warnings)
    state.trace.timed(
        "Redactor",
        started=started,
        tier="local",
        input_summary=redact_source(path, names) or "document",
        output_summary=(
            f"{len(document.text)} chars; {len(document.redactions.values)} personal detail(s) masked"
        ),
        egress="none",
        status="ok",
    )

    if document.is_empty:
        state.status = "error"
        state.warnings.append("no text extracted — nothing to flag")
        state.trace.record(
            "Extractor", tier="local", input_summary="redacted document",
            output_summary="no text", status="error",
        )
        return state

    # --- Extractor --------------------------------------------------------
    started = time.perf_counter()
    deadlines = flag_deadlines(document.text, today=today)
    state.deadlines = deadlines
    if not deadlines:
        state.status = "error"
        state.warnings.append("no dated obligation found in the document")
        state.trace.timed(
            "Extractor", started=started, tier="local",
            input_summary="redacted document", output_summary="no dated obligation",
            egress="none", status="error",
        )
        return state

    primary = pick_deadline(deadlines, today=today)
    assert primary is not None
    item = build_item(document, primary, kind=kind, source=str(path), names=names)
    state.item = item
    state.doc_id = item.item_id
    fields = extract_fields(document, primary, item=item)
    state.extracted_fields = fields
    if memory is not None:
        try:
            memory.upsert(item)
        except Exception as exc:
            state.warnings.append(f"could not store the item ({type(exc).__name__})")
    state.trace.timed(
        "Extractor",
        started=started,
        tier="local",
        input_summary="redacted document",
        output_summary=(
            f"vendor={item.provider}; deadline={primary.date.isoformat()}; "
            f"amount={item.amount if item.amount is not None else 'unknown'}; "
            f"{len(fields['line_items'])} line item(s)"
        ),
        egress="none",
        status="ok",
    )

    # --- Verifier ---------------------------------------------------------
    started = time.perf_counter()
    inventory = []
    if memory is not None:
        try:
            inventory = [
                {
                    "provider": other.provider,
                    "amount": other.amount,
                    "date": other.renewal_date,
                    "item_id": other.item_id,
                    "label": other.kind,
                }
                for other in memory.all_items()
                if other.item_id != item.item_id
            ]
        except Exception:
            inventory = []
    facts = verify(
        {
            **fields,
            "charges": [
                {
                    "provider": item.provider,
                    "amount": item.amount,
                    "date": item.renewal_date,
                    "item_id": item.item_id,
                    "label": item.kind,
                }
            ],
        },
        today=today,
        inventory=inventory,
    )
    state.verified_facts = facts
    state.trace.timed(
        "Verifier",
        started=started,
        tier="code",
        input_summary="extracted fields",
        output_summary="; ".join(facts.facts) or "no facts",
        egress="none",
        status="ok",
    )

    # --- Researcher -------------------------------------------------------
    started = time.perf_counter()
    if researcher is not None:
        query = local_redact(
            f"{item.provider} {item.kind} renewal policy cancellation rules",
            names=names,
        ).text
        try:
            notes = researcher(query)
            state.research_notes = notes
            state.trace.timed(
                "Researcher",
                started=started,
                tier="super",
                input_summary=f"redacted query: {query}",
                output_summary=f"{len(notes)} note(s)",
                egress=egress_label("api.tavily.com"),
                status="ok",
            )
        except Exception as exc:
            state.warnings.append(f"research failed ({type(exc).__name__})")
            state.trace.timed(
                "Researcher", started=started, tier="super",
                input_summary=f"redacted query: {query}",
                output_summary=f"failed: {type(exc).__name__}",
                egress=egress_label("api.tavily.com", blocked=True), status="error",
            )
    else:
        state.trace.timed(
            "Researcher",
            started=started,
            tier="none",
            input_summary="redacted vendor and issue type",
            output_summary="skipped: no external research requested",
            egress="none",
            status="ok",
        )

    # --- Drafter / Auditor loop ------------------------------------------
    if drafter is None:

        def build_draft(
            deadline: Deadline,
            household_item: HouseholdItem | None,
            _facts: VerifiedFacts,
        ) -> DraftAction:
            return draft_action(deadline, context=household_item, kind=action_kind)

    else:
        build_draft = drafter
    state.draft = build_draft(primary, item, facts)
    state.trace.record(
        "Drafter",
        tier="template",
        input_summary="verified facts",
        output_summary=state.draft.title,
        egress="none",
        status="ok",
    )

    source_spans = [span for span in (primary.evidence, *facts.facts) if span]
    for round_number in range(max_revisions + 1):
        started = time.perf_counter()
        audit = audit_draft(
            state.draft,
            facts=facts,
            source_spans=source_spans,
            item=item,
            household_names=names,
        )
        state.audit_result = audit
        state.audit_history.append(audit)
        state.trace.timed(
            "Auditor",
            started=started,
            tier="code",
            input_summary=(
                f"draft v{round_number + 1}; {len(facts.facts)} verified fact(s)"
            ),
            output_summary=(
                "approved"
                if audit.approved
                else f"rejected: {', '.join(audit.codes)}"
            ),
            egress="none",
            status="ok" if audit.approved else "rejected",
        )
        if audit.approved:
            state.status = "ok"
            break
        if round_number >= max_revisions:
            state.status = "needs_human_review"
            break

        state.retries += 1
        started = time.perf_counter()
        state.draft = revise_draft(state.draft, audit, facts=facts, item=item)
        state.trace.timed(
            "Drafter",
            started=started,
            tier="template",
            input_summary=f"revision {state.retries}: {', '.join(audit.codes)}",
            output_summary="revised draft",
            egress="none",
            status="ok",
        )

    if state.status == "pending":
        state.status = "needs_human_review"

    state.trace.record(
        "Human approval",
        tier="human",
        input_summary=f"{state.status}; {state.retries} revision(s)",
        output_summary="waiting for the user — nothing is sent automatically",
        egress="none",
        status="ok" if state.status == "ok" else "needs_human_review",
    )
    return state


def run_text(text: str, **kwargs: object) -> RunState:
    """Run the graph over pasted text via a temporary file.

    The temporary file is deleted immediately; it is the same path the
    upload endpoint uses.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", prefix="ledger-orchestrator-", suffix=".txt", delete=True, encoding="utf-8"
    ) as handle:
        handle.write(text)
        handle.flush()
        return run_document(handle.name, **kwargs)  # type: ignore[arg-type]
