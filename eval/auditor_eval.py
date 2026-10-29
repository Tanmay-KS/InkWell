#!/usr/bin/env python3
"""Auditor catch rate over the planted-error cases.

Upgrade Phase D — the fourth number.

Each case in `eval/auditor_cases/` is a synthetic, known-bad draft:

    wrong amount, uncited claim, promised outcome, wrong date,
    unexpected personal data

plus a clean control case. The harness runs the real orchestrator with a
Drafter that injects the planted error, so the measured path is the same
one the demo uses:

    Redactor -> Extractor -> Verifier -> Drafter -> Auditor
                                             reject -> revise -> Auditor

The metric is the Auditor's catch rate: cases rejected on the first pass
divided by planted cases. A false approve is a planted error that the
Auditor let through. The control case measures the opposite failure, a
false reject.

Everything here is synthetic. The notice, the person, and the provider are
invented. The number is not a claim about real correspondence.

Usage:

    python eval/auditor_eval.py
    python eval/auditor_eval.py --json eval/results/auditor_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.actions.draft_actions import ActionKind, DraftAction, draft_action
from agent.deadlines.flag_deadlines import Deadline
from agent.memory.hermes_memory import HouseholdItem
from agent.orchestrator import run_text
from agent.verifier import VerifiedFacts

__all__ = ["CASES_DIR", "CaseResult", "load_cases", "main", "run_eval"]

CASES_DIR = Path(__file__).resolve().parent / "auditor_cases"

# The corpus is generated against a fixed date so the deadline and the
# day counts do not move between runs.
TODAY = date(2026, 10, 20)

# A single synthetic notice with a component list and a total. The total
# is what a refund draft is about; the components give the Verifier
# something to sum.
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


@dataclass(slots=True)
class CaseResult:
    """One planted case and what the Auditor did with it."""

    case_id: str
    description: str
    expect: str
    expected_code: str | None
    first_decision: str
    first_codes: list[str]
    final_decision: str
    final_codes: list[str]
    retries: int
    caught: bool
    approved_after_revision: bool
    rounds_in_trace: int

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "description": self.description,
            "expect": self.expect,
            "expected_code": self.expected_code,
            "first_decision": self.first_decision,
            "first_codes": self.first_codes,
            "final_decision": self.final_decision,
            "final_codes": self.final_codes,
            "retries": self.retries,
            "caught": self.caught,
            "approved_after_revision": self.approved_after_revision,
            "rounds_in_trace": self.rounds_in_trace,
        }


def load_cases(directory: Path = CASES_DIR) -> list[dict[str, object]]:
    """Every case file, sorted by id. The control case sorts first."""
    cases = []
    for path in sorted(directory.glob("*.json")):
        cases.append(json.loads(path.read_text(encoding="utf-8")))
    return cases


def _apply_edit(body: str, edit: dict[str, object] | None) -> str:
    if not edit:
        return body
    replace = edit.get("replace")
    if isinstance(replace, list) and len(replace) == 2:
        body = body.replace(str(replace[0]), str(replace[1]))
    append = edit.get("append")
    if append:
        body = body.rstrip() + " " + str(append)
    return body


def _drafter(edit: dict[str, object] | None):
    """A Drafter that produces the clean template, then plants the error."""

    def build(
        deadline: Deadline,
        item: HouseholdItem | None,
        _facts: VerifiedFacts,
    ) -> DraftAction:
        draft = draft_action(deadline, context=item, kind=ActionKind.REFUND)
        draft.body = _apply_edit(draft.body, edit)
        return draft

    return build


def run_eval(cases: list[dict[str, object]]) -> dict[str, object]:
    """Run every case through the real orchestrator."""
    results: list[CaseResult] = []
    for case in cases:
        state = run_text(
            NOTICE,
            today=TODAY,
            action_kind=ActionKind.REFUND,
            drafter=_drafter(case.get("edit")),  # type: ignore[arg-type]
        )
        first = state.audit_history[0] if state.audit_history else None
        final = state.audit_result
        expect = str(case.get("expect", "reject"))
        expected_code = case.get("expected_code")
        first_codes = first.codes if first else []
        caught = (
            first is not None
            and first.decision.value == "reject"
            and (expected_code is None or str(expected_code) in first_codes)
        )
        if expect == "approve":
            caught = first is not None and first.decision.value == "approve"
        rounds = sum(1 for event in state.trace.events if event.agent == "Auditor")
        results.append(
            CaseResult(
                case_id=str(case.get("id", "")),
                description=str(case.get("description", "")),
                expect=expect,
                expected_code=str(expected_code) if expected_code else None,
                first_decision=first.decision.value if first else "none",
                first_codes=first_codes,
                final_decision=final.decision.value if final else "none",
                final_codes=final.codes if final else [],
                retries=state.retries,
                caught=caught,
                approved_after_revision=(
                    state.status == "ok" and state.retries >= 1
                ),
                rounds_in_trace=rounds,
            )
        )

    planted = [result for result in results if result.expect == "reject"]
    clean = [result for result in results if result.expect == "approve"]
    rejected = [result for result in planted if result.caught]
    false_approvals = [result for result in planted if not result.caught]
    false_rejects = [result for result in clean if not result.caught]
    revised = [result for result in planted if result.approved_after_revision]
    two_rounds = [
        result
        for result in planted
        if result.rounds_in_trace >= 2 and result.approved_after_revision
    ]

    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "synthetic": True,
        "method": (
            "Deterministic Auditor over the real orchestrator; each planted "
            "error is injected by a Drafter override, then the deterministic "
            "revision is re-audited."
        ),
        "notice": "synthetic",
        "case_count": len(results),
        "planted_count": len(planted),
        "clean_count": len(clean),
        "rejected_first_pass": len(rejected),
        "catch_rate": round(len(rejected) / len(planted), 4) if planted else 0.0,
        "false_approvals": len(false_approvals),
        "false_approve_ids": [result.case_id for result in false_approvals],
        "false_rejects": len(false_rejects),
        "false_reject_ids": [result.case_id for result in false_rejects],
        "approved_after_revision": len(revised),
        "two_rounds_in_trace": len(two_rounds),
        "cases": [result.to_dict() for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", default=str(CASES_DIR))
    parser.add_argument("--json", default="eval/results/auditor_eval.json")
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.cases))
    if not cases:
        print(f"no cases in {args.cases}", file=sys.stderr)
        return 2

    result = run_eval(cases)
    output = Path(args.json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        f"planted {result['planted_count']}, rejected {result['rejected_first_pass']}, "
        f"catch rate {result['catch_rate']:.1%}, "
        f"false approves {result['false_approvals']}, "
        f"false rejects {result['false_rejects']}, "
        f"approved after revision {result['approved_after_revision']}"
    )
    for case in result["cases"]:
        print(
            f"  {case['case_id']:34s} first={case['first_decision']:7s} "
            f"final={case['final_decision']:7s} codes={','.join(case['first_codes']) or '-'}"
        )
    print(f"artifact: {output}")
    return 0 if result["catch_rate"] == 1.0 and result["false_rejects"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
