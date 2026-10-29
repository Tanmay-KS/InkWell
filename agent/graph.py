"""The LangGraph role graph.

Upgrade Phase B — the framework path.

The plan's gate was time-boxed: build a small graph with a Drafter, an
Auditor, and a conditional edge back to the Drafter. It works, so this
module is the graph implementation of the same roles the plain-Python
orchestrator exposes:

    START -> Redactor -> Extractor -> Verifier -> Researcher -> Drafter
                                                     ^             |
                                                     |          Auditor
                                                     +-- reject ---+
                                                          (max 2)

The important properties are unchanged from the fallback:

* the Redactor runs **first**, and the Extractor, Drafter and Auditor only
  ever see the redacted text;
* the Verifier is plain code (totals, duplicates, days, expiry);
* the Auditor is the same deterministic veto and cannot approve a draft
  whose amount or date disagrees with the Verifier;
* the revision loop is capped at two, after which the run stops at
  `needs_human_review`;
* the user still approves every outbound action — there is no send path.

LangGraph is a dependency now. `langsmith` arrives transitively through
`langchain-core`, so tracing is disabled explicitly below, before the
import. No LangSmith key is read, and the trace the project shows is
`agent/trace.py`, not a hosted service.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TypedDict

# Must happen before `langgraph` / `langchain_core` are imported. The
# project's trace is local; a transitive dependency must not start
# shipping run data to a third party.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGCHAIN_TRACING"] = "false"

from langgraph.graph import END, START, StateGraph

from agent.actions.draft_actions import ActionKind, DraftAction, draft_action
from agent.auditor import AuditResult, audit_draft, revise_draft
from agent.deadlines.flag_deadlines import Deadline, flag_deadlines
from agent.ingestion.parse_documents import Document, parse_document
from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory
from agent.model import ModelReply, NebiusClient
from agent.orchestrator import MAX_REVISIONS, RunState, extract_fields
from agent.pipeline import build_item, pick_deadline, redact_source
from agent.trace import TraceLog, egress_label
from agent.verifier import VerifiedFacts, verify
from privacy_router.egress import Transport
from privacy_router.redact import local_redact

__all__ = [
    "Drafter",
    "Researcher",
    "build_graph",
    "run_document_graph",
    "run_text_graph",
]

Drafter = Callable[[Deadline, HouseholdItem | None, VerifiedFacts], DraftAction]
Researcher = Callable[[str], list[dict[str, object]]]

_DRAFTER_SYSTEM = (
    "You draft a short, plain letter for a household member to review and send. "
    "Use only the verified facts and the notice text supplied. Never invent an "
    "amount, date, policy term, or outcome. Do not promise that a refund or "
    "approval will happen, and do not give legal or financial advice. Write in "
    "the first person, include the reference and date that are supplied, and "
    "end with a specific request. Return only the letter body."
)


class GraphState(TypedDict, total=False):
    """The shared state passed between LangGraph nodes."""

    run_id: str
    path: str
    redacted_text: str
    document: Document
    item: HouseholdItem
    deadlines: list[Deadline]
    extracted_fields: dict[str, object]
    verified_facts: VerifiedFacts
    research_notes: list[dict[str, object]]
    draft: DraftAction
    audit_result: AuditResult
    audit_history: list[AuditResult]
    retries: int
    status: str
    warnings: list[str]
    source_spans: list[str]
    trace: TraceLog


def _inventory(memory: HouseholdMemory | None, item: HouseholdItem) -> list[dict[str, object]]:
    if memory is None:
        return []
    try:
        return [
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
        return []


def _model_draft(
    client: NebiusClient,
    document: Document,
    deadline: Deadline,
    item: HouseholdItem,
    facts: VerifiedFacts,
    action_kind: ActionKind | None,
) -> tuple[DraftAction, ModelReply]:
    """Ask Nemotron Super for a letter, then resolve placeholders locally.

    The model reads the **redacted** notice. The real policy number and
    holder go back into the draft only here, inside the sandbox, through
    the redaction map — the same trapdoor the template path uses.
    """
    facts_block = "\n".join(f"- {fact}" for fact in facts.facts)
    user = (
        f"Action: {(action_kind or ActionKind.RENEW).value}\n"
        f"Provider: {item.provider}\n"
        f"Deadline: {deadline.date.isoformat()} ({deadline.kind.value})\n"
        f"Verified facts:\n{facts_block}\n\n"
        f"Redacted notice:\n{document.text[:4000]}"
    )
    reply = client.complete("super", system=_DRAFTER_SYSTEM, user=user)
    body = document.redactions.resolve(reply.text).strip()

    known = [value for value in document.redactions.values.values() if value and value in body]
    for value in (item.reference, item.holder):
        if value and value in body and value not in known:
            known.append(value)

    return (
        DraftAction(
            kind=action_kind or ActionKind.RENEW,
            subject=item.item_id,
            title=f"Draft for {item.provider}",
            body=body,
            recipient=item.provider,
            personal_details=known,
            metadata={
                "source": "nemotron-super",
                "model": reply.model,
                "tokens_in": reply.tokens_in,
                "tokens_out": reply.tokens_out,
            },
        ),
        reply,
    )


def build_graph(
    *,
    memory: HouseholdMemory | None = None,
    today: date | None = None,
    kind: str = "",
    action_kind: ActionKind | None = None,
    redact: bool = True,
    transport: Transport | None = None,
    drafter: Drafter | None = None,
    researcher: Researcher | None = None,
    model_client: NebiusClient | None = None,
    use_model_drafter: bool = False,
    max_revisions: int = MAX_REVISIONS,
    names: list[str] | None = None,
):
    """Compile the role graph.

    `drafter` overrides the Drafter (the planted-error demo uses this).
    `use_model_drafter` turns on the Nemotron Super path; it requires
    `model_client`. The Auditor and Verifier are always code.
    """
    today = today or date.today()
    household_names = list(names or [])
    client = model_client

    def redactor_node(state: GraphState) -> GraphState:
        started = time.perf_counter()
        trace: TraceLog = state["trace"]
        try:
            document = parse_document(
                state["path"],
                model=None,
                transport=transport,
                household_names=household_names,
                redact=redact,
            )
        except Exception as exc:
            trace.record(
                "Redactor",
                tier="local",
                latency_ms=int((time.perf_counter() - started) * 1000),
                input_summary=redact_source(state["path"], household_names) or "document",
                output_summary=f"failed: {type(exc).__name__}",
                status="error",
            )
            return {"status": "error", "warnings": [f"extraction failed: {type(exc).__name__}"]}

        trace.record(
            "Redactor",
            tier="local",
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_summary=redact_source(state["path"], household_names) or "document",
            output_summary=(
                f"{len(document.text)} chars; "
                f"{len(document.redactions.values)} personal detail(s) masked"
            ),
            egress="none",
        )
        if document.is_empty:
            trace.record(
                "Extractor",
                tier="local",
                output_summary="no text",
                status="error",
            )
            return {
                "document": document,
                "redacted_text": document.text,
                "status": "error",
                "warnings": [*list(document.warnings), "no text extracted — nothing to flag"],
            }
        return {
            "document": document,
            "redacted_text": document.text,
            "status": "running",
            "warnings": list(document.warnings),
        }

    def extractor_node(state: GraphState) -> GraphState:
        started = time.perf_counter()
        trace: TraceLog = state["trace"]
        document = state["document"]
        deadlines = flag_deadlines(document.text, today=today)
        if not deadlines:
            trace.record(
                "Extractor",
                tier="local",
                latency_ms=int((time.perf_counter() - started) * 1000),
                input_summary="redacted document",
                output_summary="no dated obligation",
                status="error",
            )
            return {
                "deadlines": [],
                "status": "error",
                "warnings": [*list(state.get("warnings") or []), "no dated obligation found in the document"],
            }

        primary = pick_deadline(deadlines, today=today)
        assert primary is not None
        item = build_item(document, primary, kind=kind, source=state["path"], names=household_names)
        fields = extract_fields(document, primary, item=item)
        if memory is not None:
            try:
                memory.upsert(item)
            except Exception as exc:
                state.setdefault("warnings", []).append(
                    f"could not store the item ({type(exc).__name__})"
                )

        trace.record(
            "Extractor",
            tier="local",
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_summary="redacted document",
            output_summary=(
                f"vendor={item.provider}; deadline={primary.date.isoformat()}; "
                f"amount={item.amount if item.amount is not None else 'unknown'}; "
                f"{len(fields['line_items'])} line item(s)"
            ),
            egress="none",
        )
        return {
            "item": item,
            "deadlines": deadlines,
            "extracted_fields": fields,
            "status": "running",
        }

    def verifier_node(state: GraphState) -> GraphState:
        started = time.perf_counter()
        trace: TraceLog = state["trace"]
        item = state["item"]
        facts = verify(
            {
                **state["extracted_fields"],
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
            inventory=_inventory(memory, item),
        )
        trace.record(
            "Verifier",
            tier="code",
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_summary="extracted fields",
            output_summary="; ".join(facts.facts) or "no facts",
            egress="none",
        )
        return {"verified_facts": facts, "status": "running"}

    def researcher_node(state: GraphState) -> GraphState:
        started = time.perf_counter()
        trace: TraceLog = state["trace"]
        item = state["item"]
        if researcher is None:
            trace.record(
                "Researcher",
                tier="none",
                latency_ms=int((time.perf_counter() - started) * 1000),
                input_summary="redacted vendor and issue type",
                output_summary="skipped: no external research requested",
                egress="none",
            )
            return {"research_notes": [], "status": "running"}

        query = local_redact(
            f"{item.provider} {item.kind} renewal policy cancellation rules",
            names=household_names,
        ).text
        try:
            notes = researcher(query)
        except Exception as exc:
            trace.record(
                "Researcher",
                tier="super",
                latency_ms=int((time.perf_counter() - started) * 1000),
                input_summary=f"redacted query: {query}",
                output_summary=f"failed: {type(exc).__name__}",
                egress=egress_label("api.tavily.com", blocked=True),
                status="error",
            )
            return {
                "research_notes": [],
                "status": "running",
                "warnings": [*list(state.get("warnings") or []), f"research failed ({type(exc).__name__})"],
            }
        trace.record(
            "Researcher",
            tier="super",
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_summary=f"redacted query: {query}",
            output_summary=f"{len(notes)} note(s)",
            egress=egress_label("api.tavily.com"),
        )
        return {"research_notes": notes, "status": "running"}

    def drafter_node(state: GraphState) -> GraphState:
        trace: TraceLog = state["trace"]
        primary = state["deadlines"][0]
        item = state["item"]
        facts = state["verified_facts"]
        prior = state.get("audit_result")

        # A rejection sends the run back here. The repair is deterministic:
        # replace a wrong amount/date, drop an unsupported sentence, mask
        # unexpected personal data. Re-asking the model would risk a new
        # error with no new evidence.
        if prior is not None and not prior.approved and state.get("retries", 0) > 0:
            started = time.perf_counter()
            revised = revise_draft(prior and state["draft"], prior, facts=facts, item=item)
            trace.record(
                "Drafter",
                tier="template",
                latency_ms=int((time.perf_counter() - started) * 1000),
                input_summary=f"revision {state['retries']}: {', '.join(prior.codes)}",
                output_summary="revised draft",
                egress="none",
                status="ok",
            )
            return {"draft": revised, "status": "running"}

        if drafter is not None:
            started = time.perf_counter()
            built = drafter(primary, item, facts)
            trace.record(
                "Drafter",
                tier="template",
                latency_ms=int((time.perf_counter() - started) * 1000),
                input_summary="verified facts",
                output_summary=built.title,
                egress="none",
            )
            return {"draft": built, "status": "running"}

        if use_model_drafter:
            if client is None:
                trace.record(
                    "Drafter",
                    tier="super",
                    output_summary="model drafter requested without a client",
                    status="error",
                )
                return {
                    "status": "error",
                    "warnings": [
                        *list(state.get("warnings") or []),
                        "model drafter requested without a Token Factory client",
                    ],
                }
            try:
                built, reply = _model_draft(
                    client, state["document"], primary, item, facts, action_kind
                )
            except Exception as exc:
                trace.record(
                    "Drafter",
                    tier="super",
                    input_summary="verified facts + redacted notice",
                    output_summary=f"model call failed: {type(exc).__name__}",
                    egress=egress_label("api.tokenfactory.nebius.com", blocked=True),
                    status="error",
                )
                return {
                    "status": "error",
                    "warnings": [
                        *list(state.get("warnings") or []),
                        f"model drafting failed ({type(exc).__name__})",
                    ],
                }
            trace.record(
                "Drafter",
                model=reply.model,
                tier="super",
                latency_ms=reply.latency_ms,
                tokens_in=reply.tokens_in,
                tokens_out=reply.tokens_out,
                input_summary="verified facts + redacted notice",
                output_summary=built.title,
                egress=egress_label("api.tokenfactory.nebius.com"),
            )
            return {"draft": built, "status": "running"}

        started = time.perf_counter()
        built = draft_action(primary, context=item, kind=action_kind)
        trace.record(
            "Drafter",
            tier="template",
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_summary="verified facts",
            output_summary=built.title,
            egress="none",
        )
        return {"draft": built, "status": "running"}

    def auditor_node(state: GraphState) -> GraphState:
        trace: TraceLog = state["trace"]
        facts = state["verified_facts"]
        item = state["item"]
        primary = state["deadlines"][0]
        source_spans = [span for span in (primary.evidence, *facts.facts) if span]
        history = list(state.get("audit_history") or [])
        round_number = len(history) + 1

        started = time.perf_counter()
        audit = audit_draft(
            state["draft"],
            facts=facts,
            source_spans=source_spans,
            item=item,
            household_names=household_names,
        )
        history.append(audit)
        trace.record(
            "Auditor",
            tier="code",
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_summary=f"draft v{round_number}; {len(facts.facts)} verified fact(s)",
            output_summary=(
                "approved" if audit.approved else f"rejected: {', '.join(audit.codes)}"
            ),
            egress="none",
            status="ok" if audit.approved else "rejected",
        )

        if audit.approved:
            return {
                "audit_result": audit,
                "audit_history": history,
                "source_spans": source_spans,
                "status": "ok",
            }
        retries = int(state.get("retries", 0))
        if retries >= max_revisions:
            return {
                "audit_result": audit,
                "audit_history": history,
                "source_spans": source_spans,
                "status": "needs_human_review",
            }
        return {
            "audit_result": audit,
            "audit_history": history,
            "source_spans": source_spans,
            "retries": retries + 1,
            "status": "running",
        }

    def after_redactor(state: GraphState) -> str:
        return "end" if state.get("status") == "error" else "extractor"

    def after_extractor(state: GraphState) -> str:
        return "end" if state.get("status") == "error" else "verifier"

    def after_drafter(state: GraphState) -> str:
        return "end" if state.get("status") == "error" else "auditor"

    def after_auditor(state: GraphState) -> str:
        return "end" if state.get("status") in {"ok", "needs_human_review", "error"} else "drafter"

    graph = StateGraph(GraphState)
    graph.add_node("redactor", redactor_node)
    graph.add_node("extractor", extractor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("researcher", researcher_node)
    graph.add_node("drafter", drafter_node)
    graph.add_node("auditor", auditor_node)

    graph.add_edge(START, "redactor")
    graph.add_conditional_edges("redactor", after_redactor, {"extractor": "extractor", "end": END})
    graph.add_conditional_edges(
        "extractor", after_extractor, {"verifier": "verifier", "end": END}
    )
    graph.add_edge("verifier", "researcher")
    graph.add_edge("researcher", "drafter")
    graph.add_conditional_edges("drafter", after_drafter, {"auditor": "auditor", "end": END})
    graph.add_conditional_edges("auditor", after_auditor, {"drafter": "drafter", "end": END})
    return graph.compile()


def _to_run_state(final: GraphState, *, path: str, trace: TraceLog) -> RunState:
    document = final.get("document")
    item = final.get("item")
    state = RunState(
        doc_id=item.item_id if item is not None else final.get("run_id", uuid.uuid4().hex[:12]),
        path=path,
        trace=trace,
        redacted_text=final.get("redacted_text", ""),
        extracted_fields=dict(final.get("extracted_fields") or {}),
        verified_facts=final.get("verified_facts"),
        research_notes=list(final.get("research_notes") or []),
        draft=final.get("draft"),
        audit_result=final.get("audit_result"),
        retries=int(final.get("retries", 0)),
        status=final.get("status", "pending"),
        document=document,
        item=item,
        deadlines=list(final.get("deadlines") or []),
        audit_history=list(final.get("audit_history") or []),
        warnings=list(final.get("warnings") or []),
    )
    if state.status == "pending":
        state.status = "needs_human_review"
    trace.record(
        "Human approval",
        tier="human",
        input_summary=f"{state.status}; {state.retries} revision(s)",
        output_summary="waiting for the user — nothing is sent automatically",
        egress="none",
        status="ok" if state.status == "ok" else "needs_human_review",
    )
    return state


def _names(memory: HouseholdMemory | None) -> list[str]:
    if memory is None:
        return []
    try:
        return memory.household_names()
    except Exception:
        return []


def run_document_graph(
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
    model_client: NebiusClient | None = None,
    use_model_drafter: bool = False,
    max_revisions: int = MAX_REVISIONS,
) -> RunState:
    """Run the LangGraph role graph over one document.

    The signature mirrors `agent.orchestrator.run_document` so a caller can
    switch between the framework path and the documented fallback without
    changing the rest of its code.
    """
    trace = TraceLog(path=Path(trace_path) if trace_path else None)
    names = _names(memory)
    app = build_graph(
        memory=memory,
        today=today,
        kind=kind,
        action_kind=action_kind,
        redact=redact,
        transport=transport,
        drafter=drafter,
        researcher=researcher,
        model_client=model_client,
        use_model_drafter=use_model_drafter,
        max_revisions=max_revisions,
        names=names,
    )
    initial: GraphState = {
        "run_id": trace.run_id,
        "path": str(path),
        "retries": 0,
        "status": "pending",
        "warnings": [],
        "trace": trace,
    }
    final: GraphState = app.invoke(initial, config={"recursion_limit": 50})
    return _to_run_state(final, path=str(path), trace=trace)


def run_text_graph(text: str, **kwargs: object) -> RunState:
    """Run the graph over pasted text via a temporary file."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", prefix="ledger-graph-", suffix=".txt", delete=True, encoding="utf-8"
    ) as handle:
        handle.write(text)
        handle.flush()
        return run_document_graph(handle.name, **kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover - manual gate check
    import argparse

    parser = argparse.ArgumentParser(description="Run the LangGraph role graph on one document")
    parser.add_argument("document")
    parser.add_argument("--model-drafter", action="store_true", help="use Nemotron Super to draft")
    args = parser.parse_args()

    result = run_document_graph(
        args.document,
        use_model_drafter=args.model_drafter,
        model_client=NebiusClient() if args.model_drafter else None,
    )
    print(f"status={result.status} retries={result.retries}")
    for event in result.trace.events:
        print(f"{event.step:2d} {event.agent:16s} {event.tier:8s} {event.status:18s} {event.output_summary}")
