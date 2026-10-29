#!/usr/bin/env python3
"""The LangGraph gate: does the framework path work with a real model?

Upgrade Phase B / Upgrade 3.

The plan's gate was time-boxed to 60 minutes. This script answers the
question the gate was really asking: does a compiled LangGraph pipeline
run the roles, call a Token Factory Nemotron model, and still let the
deterministic Auditor veto a bad draft?

It runs the **real** graph over a synthetic notice:

    Redactor -> Extractor -> Verifier -> Researcher -> Drafter (Nemotron
    Super) -> Auditor -> conditional edge -> END

Nothing is sent anywhere except one chat-completion request to Token
Factory. The notice, the person and the provider are invented.

The artifact is written to `eval/results/langgraph_gate.json` and is a
synthetic test result, not a general benchmark.

Usage:

    python eval/langgraph_gate.py
    python eval/langgraph_gate.py --no-model   # compile + template only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.actions.draft_actions import ActionKind
from agent.graph import build_graph, run_text_graph
from agent.model import NebiusClient, pinned_model

__all__ = ["NOTICE", "main", "run_gate"]

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


def _trace_summary(state) -> dict[str, object]:
    return {
        "status": state.status,
        "retries": state.retries,
        "steps": [
            {
                "step": event.step,
                "agent": event.agent,
                "tier": event.tier,
                "model": event.model,
                "status": event.status,
                "latency_ms": event.latency_ms,
                "tokens_in": event.tokens_in,
                "tokens_out": event.tokens_out,
                "egress": event.egress,
                "output_summary": event.output_summary,
            }
            for event in state.trace.events
        ],
        "auditor_decisions": [
            event.status for event in state.trace.events if event.agent == "Auditor"
        ],
        "model_calls": state.trace.summary()["model_calls"],
        "warnings": list(state.warnings),
    }


def run_gate(*, use_model: bool = True) -> dict[str, object]:
    """Run the graph and return the artifact body."""
    # A compile check independent of any model call: if the graph cannot
    # be built, nothing below can be trusted.
    build_graph(today=TODAY, action_kind=ActionKind.REFUND)

    client = NebiusClient() if use_model else None
    model_id = None
    if use_model:
        model_id = pinned_model("super")
        if not client.available():  # type: ignore[union-attr]
            raise RuntimeError("NEBIUS_API_KEY is not set")

    state = run_text_graph(
        NOTICE,
        today=TODAY,
        action_kind=ActionKind.REFUND,
        use_model_drafter=use_model,
        model_client=client,
    )
    summary = _trace_summary(state)
    drafter = next((step for step in summary["steps"] if step["agent"] == "Drafter"), None)

    return {
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "synthetic": True,
        "framework": "langgraph",
        "framework_import": "langgraph.graph.StateGraph",
        "model_requested": use_model,
        "model_pinned": model_id,
        "compiled": True,
        **summary,
        "drafter": drafter,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-model", action="store_true", help="compile and use the template Drafter")
    parser.add_argument("--json", default="eval/results/langgraph_gate.json")
    args = parser.parse_args(argv)

    try:
        result = run_gate(use_model=not args.no_model)
    except Exception as exc:
        print(f"gate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    output = Path(args.json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        f"langgraph compiled; status={result['status']}; "
        f"model_calls={result['model_calls']}; "
        f"auditor={result['auditor_decisions']}"
    )
    drafter = result.get("drafter") or {}
    if result["model_requested"]:
        print(
            f"drafter model={drafter.get('model')} tokens_in={drafter.get('tokens_in')} "
            f"tokens_out={drafter.get('tokens_out')} latency_ms={drafter.get('latency_ms')} "
            f"egress={drafter.get('egress')}"
        )
    print(f"artifact: {output}")

    # The gate passes when the graph ran, the model call succeeded (if
    # requested), and the Auditor actually reviewed the draft. A rejection
    # followed by a revision is still a pass; a run that skipped the model
    # or the Auditor is not.
    ok = (
        result["compiled"] is True
        and result["auditor_decisions"] != []
        and (not result["model_requested"] or result["model_calls"] >= 1)
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
