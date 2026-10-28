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
catches an agent that has learned to always produce an answer. It is also
why the query runs for every answer type, including `never_stated`: a
short-circuit that returns "" without asking the store would score 100%
without testing anything.

Scenarios are also tagged with a category (status, amount, renewal_date,
holder, contact, preference, confidence, restatement) so the report can
show *which kind* of update the system handles, not just an overall rate.

The scenarios live in `eval/scenarios/`. See `docs/phase3-runbook.md` §3.

Usage:

    python eval/memory_updates_eval.py
    python eval/memory_updates_eval.py --scenarios eval/scenarios --json eval/results/memory_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.memory.hermes_memory import HouseholdMemory
from agent.memory.update_tracker import Fact, FactKind, UpdateTracker

__all__ = [
    "Scenario",
    "ScenarioResult",
    "load_scenarios",
    "main",
    "run_eval",
    "score",
    "score_by_category",
]

DEFAULT_KIND = FactKind.SUBSCRIPTION_STATUS.value


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
        metadata: What to query — `subject`, `kind`, `as_of`, `category`.
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
    category: str = ""
    notes: str = ""


def _subject_kind(scenario: Scenario) -> tuple[str, FactKind]:
    subject = str(scenario.metadata.get("subject", ""))
    kind = FactKind(str(scenario.metadata.get("kind", DEFAULT_KIND)))
    return subject, kind


def _as_of(scenario: Scenario) -> date | None:
    raw = scenario.metadata.get("as_of")
    return date.fromisoformat(raw) if raw else None


def _answer(tracker: UpdateTracker, scenario: Scenario) -> str:
    """Read the store as of the scenario's date.

    Every answer type goes through this. `never_stated` is not a special
    case that skips the query — the absence of a fact is the thing being
    tested.
    """
    subject, kind = _subject_kind(scenario)
    if not subject:
        return ""
    fact = tracker.current(subject, kind, when=_as_of(scenario))
    return fact.value if fact else ""


def run_eval(scenarios: list[Scenario]) -> list[ScenarioResult]:
    """Run the memory evaluation.

    Args:
        scenarios: The scenarios to run.

    Returns:
        One result per scenario.

    The deterministic SQLite tracker is the system under test. This
    measures update semantics and history, not language-model paraphrase
    quality — a model in the loop would make the number about the model
    rather than about the memory layer.
    """
    results = []
    for scenario in scenarios:
        with TemporaryDirectory(prefix="ledger-memory-eval-") as tmp, HouseholdMemory(
            Path(tmp) / "memory.db"
        ) as memory:
            tracker = UpdateTracker(memory)
            for statement in scenario.statements:
                tracker.record(
                    Fact(
                        subject=statement["subject"],
                        kind=FactKind(statement["kind"]),
                        value=str(statement["value"]),
                        valid_from=date.fromisoformat(statement["valid_from"]),
                        source=statement.get("source", scenario.scenario_id),
                        confidence=float(statement.get("confidence", 1.0)),
                    )
                )

            answer = _answer(tracker, scenario)
            abstained = not answer
            expected = scenario.expected
            results.append(
                ScenarioResult(
                    scenario_id=scenario.scenario_id,
                    answer_type=scenario.answer_type,
                    category=str(scenario.metadata.get("category", "uncategorised")),
                    correct=(
                        abstained
                        if scenario.answer_type == "never_stated"
                        else answer.casefold() == expected.casefold()
                    ),
                    abstained=abstained,
                    answer=answer,
                    expected=expected,
                )
            )
    return results


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Load scenarios from a file or every `*.json` in a directory."""
    source = Path(path)
    files = sorted(source.glob("*.json")) if source.is_dir() else [source]
    scenarios = []
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data["scenarios"]
        scenarios.extend(Scenario(**row) for row in rows)

    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.scenario_id in seen:
            raise ValueError(f"duplicate scenario id: {scenario.scenario_id}")
        seen.add(scenario.scenario_id)
    return scenarios


def _accuracy(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def score(results: list[ScenarioResult]) -> dict[str, float]:
    """Aggregate results into per-type and overall accuracy.

    A "never_stated" question answered with a confident wrong value counts
    as wrong. An abstention counts as correct for that type only.
    """
    by_type: dict[str, list[bool]] = {}
    for result in results:
        by_type.setdefault(result.answer_type, []).append(result.correct)

    summary = {f"accuracy_{key}": _accuracy(flags) for key, flags in by_type.items()}
    summary["accuracy_overall"] = _accuracy([result.correct for result in results])
    return summary


def score_by_category(
    results: list[ScenarioResult],
) -> dict[str, dict[str, float | int]]:
    """Accuracy per update category, with the scenario count.

    A category rate over three scenarios is not a rate. The count is part
    of the result so a reader can see the denominator.
    """
    by_category: dict[str, list[bool]] = {}
    for result in results:
        by_category.setdefault(result.category or "uncategorised", []).append(result.correct)

    return {
        category: {
            "scenarios": len(flags),
            "correct": sum(flags),
            "accuracy": _accuracy(flags),
        }
        for category, flags in sorted(by_category.items())
    }


def _baseline_results(scenarios: list[Scenario]) -> list[ScenarioResult]:
    """A recall-only baseline that keeps the first value and cannot revise it.

    This is the number the memory layer has to beat. A system that
    remembers the first thing it was told is not a system with updates; it
    is the demo everyone else ships.
    """
    baseline = []
    for scenario in scenarios:
        seen: dict[tuple[str, str], str] = {}
        for statement in scenario.statements:
            seen.setdefault(
                (statement["subject"], statement["kind"]), str(statement["value"])
            )

        subject, kind = _subject_kind(scenario)
        value = seen.get((subject, kind.value), "")
        baseline.append(
            ScenarioResult(
                scenario_id=scenario.scenario_id,
                answer_type=scenario.answer_type,
                category=str(scenario.metadata.get("category", "uncategorised")),
                correct=(
                    not value
                    if scenario.answer_type == "never_stated"
                    else value.casefold() == scenario.expected.casefold()
                ),
                abstained=not value,
                answer=value,
                expected=scenario.expected,
            )
        )
    return baseline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenarios", default="eval/scenarios")
    parser.add_argument("--json", dest="json_out", default="eval/results/memory_eval.json")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    results = run_eval(scenarios)
    report = {
        "method": "deterministic SQLite fact supersession; no model call",
        "scenario_count": len(results),
        "scores": score(results),
        "by_category": score_by_category(results),
        "results": [result.__dict__ for result in results],
        "update_blind_baseline": score(_baseline_results(scenarios)),
    }
    destination = Path(args.json_out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "scenario_count": report["scenario_count"],
                **report["scores"],
            },
            indent=2,
        )
    )
    for category, stats in report["by_category"].items():
        print(f"  {category:14s} {stats['correct']}/{stats['scenarios']}  {stats['accuracy']:.1%}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
