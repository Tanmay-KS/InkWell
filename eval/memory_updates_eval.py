#!/usr/bin/env python3
"""Memory-with-updates accuracy.

Phase 3 — Person B.

Most agent memory demos test recall: *do you remember that I have a gym
membership?* That is the easy case. The plan is explicit that it is not
enough.

The hard case is a fact that changed. The user cancels the plan in March.
The agent must now believe the plan is cancelled — not believe both the
old fact and the new one, and not keep flagging a renewal for something
cancelled five months ago.

The evaluation is built from scenarios, not from documents. Each scenario
has:

* a sequence of statements, some of which supersede earlier ones,
* a question whose correct answer depends on which statement is current,
* the expected answer.

Three answer types are scored, because they fail in different ways:

* **Current value** — "is the plan active or cancelled?" A model that
  remembers both answers says both, and is wrong.
* **Superseded value** — "what was the plan before it was cancelled?" A
  model that overwrote the old fact cannot answer this, and a system that
  keeps only the current value has thrown away history it may need.
* **No hallucination** — questions about things that were never stated.
  A confident wrong answer here is worse than an abstention, so an
  abstention counts as correct.

The third type is the one most evaluations omit, and it is the one that
catches an agent that has learned to always produce an answer.

The scenarios live in `eval/scenarios/`. Phase 3 implementation.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Scenario", "ScenarioResult", "main", "run_eval"]


@dataclass
class Scenario:
    """One memory scenario.

    Attributes:
        scenario_id: Stable identifier.
        statements: The facts, in order. Later ones may supersede earlier.
        question: The question to ask.
        expected: The expected answer.
        answer_type: "current" | "superseded" | "never_stated".
        household: The synthetic household the scenario belongs to.
    """

    scenario_id: str
    statements: list[dict]
    question: str
    expected: str
    answer_type: str
    household: str = "default"
    metadata: dict = field(default_factory=dict)


@dataclass
class ScenarioResult:
    """The outcome of one scenario."""

    scenario_id: str
    answer_type: str
    correct: bool
    abstained: bool = False
    answer: str = ""
    expected: str = ""
    notes: str = ""


def run_eval(
    scenarios: list[Scenario], *, model: str | None = None
) -> list[ScenarioResult]:
    """Run the memory evaluation.

    Args:
        scenarios: The scenarios to run.
        model: The model to evaluate. Defaults to `LEDGER_MODEL_SUPER`.

    Returns:
        One result per scenario.

    Phase 3 implementation.
    """
    raise NotImplementedError("Phase 3 — see IMPLEMENTATION.md")


def score(results: list[ScenarioResult]) -> dict[str, float]:
    """Aggregate results into per-type and overall accuracy.

    A "never_stated" question answered with a confident wrong value counts
    as wrong. An abstention counts as correct for that type only.
    """
    by_type: dict[str, list[bool]] = {}
    for r in results:
        by_type.setdefault(r.answer_type, []).append(r.correct)

    summary = {f"accuracy_{k}": sum(v) / len(v) for k, v in by_type.items() if v}
    all_correct = [r.correct for r in results]
    summary["accuracy_overall"] = sum(all_correct) / len(all_correct) if all_correct else 0.0
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenarios", default="eval/scenarios")
    parser.add_argument("--json", dest="json_out", default="eval/results/memory_eval.json")
    args = parser.parse_args(argv)

    print("Phase 3 — not implemented yet. See IMPLEMENTATION.md.")
    print(f"scenarios would be loaded from {args.scenarios}")
    print(f"results would be written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
