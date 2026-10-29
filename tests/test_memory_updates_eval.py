"""Tests for the Phase 3 memory-with-updates evaluation.

The eval is only worth quoting if the scenarios actually exercise
supersession and the "never stated" type actually asks the store. Both
failures are silent: a short-circuited abstention scores 100% without
testing anything, and a baseline that is not update-blind makes the
comparison look flattering for no reason.

The canonical scenario set is the regression test: if the tracker stops
superseding a fact, `test_the_canonical_set_is_reportable` fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.memory_updates_eval import (
    Scenario,
    ScenarioResult,
    load_scenarios,
    main,
    run_eval,
    score,
    score_by_category,
)

SCENARIOS = Path("eval/scenarios")


def _scenario(**overrides) -> Scenario:
    base = {
        "scenario_id": "t",
        "statements": [
            {"subject": "gym", "kind": "subscription_status", "value": "active", "valid_from": "2026-01-01"},
            {"subject": "gym", "kind": "subscription_status", "value": "cancelled", "valid_from": "2026-03-01"},
        ],
        "question": "status?",
        "expected": "cancelled",
        "answer_type": "current",
        "metadata": {
            "subject": "gym",
            "kind": "subscription_status",
            "as_of": "2026-10-20",
            "category": "status",
        },
    }
    base.update(overrides)
    return Scenario(**base)


def test_the_canonical_set_is_reportable():
    scenarios = load_scenarios(SCENARIOS)

    assert len(scenarios) >= 20
    assert len({scenario.scenario_id for scenario in scenarios}) == len(scenarios)

    results = run_eval(scenarios)
    assert all(result.correct for result in results), [
        result.scenario_id for result in results if not result.correct
    ]
    assert score(results)["accuracy_overall"] == 1.0


def test_never_stated_actually_queries_the_store():
    # The subject *does* have a fact, but the scenario claims it was never
    # stated. A short-circuit would answer "" and score this correct. The
    # real query returns the fact, and the scenario fails — which is the
    # behaviour that proves the type is testing something.
    scenario = _scenario(
        scenario_id="bad-never",
        answer_type="never_stated",
        expected="",
        metadata={
            "subject": "gym",
            "kind": "subscription_status",
            "as_of": "2026-10-20",
            "category": "status",
        },
    )

    result = run_eval([scenario])[0]

    assert result.answer == "cancelled"
    assert result.correct is False


def test_a_superseded_query_reads_history_not_the_current_value():
    scenario = _scenario(
        answer_type="superseded",
        expected="active",
        metadata={
            "subject": "gym",
            "kind": "subscription_status",
            "as_of": "2026-02-01",
            "category": "status",
        },
    )

    result = run_eval([scenario])[0]

    assert result.answer == "active"
    assert result.correct is True


def test_the_baseline_is_update_blind():
    scenarios = load_scenarios(SCENARIOS)
    from eval.memory_updates_eval import _baseline_results

    baseline = score(_baseline_results(scenarios))

    assert baseline["accuracy_current"] < 0.5
    assert baseline["accuracy_overall"] < score(run_eval(scenarios))["accuracy_overall"]


def test_category_scores_carry_their_denominator():
    results = [
        ScenarioResult("a", "current", True, category="status"),
        ScenarioResult("b", "current", False, category="status"),
        ScenarioResult("c", "current", True, category="amount"),
    ]

    by_category = score_by_category(results)

    assert by_category["status"] == {"scenarios": 2, "correct": 1, "accuracy": 0.5}
    assert by_category["amount"] == {"scenarios": 1, "correct": 1, "accuracy": 1.0}


def test_duplicate_scenario_ids_are_rejected(tmp_path):
    row = {
        "scenario_id": "same",
        "statements": [
            {"subject": "gym", "kind": "subscription_status", "value": "active", "valid_from": "2026-01-01"}
        ],
        "question": "?",
        "expected": "active",
        "answer_type": "current",
        "metadata": {"subject": "gym", "kind": "subscription_status"},
    }
    path = tmp_path / "one.json"
    path.write_text(json.dumps({"scenarios": [row, dict(row)]}))

    with pytest.raises(ValueError, match="duplicate scenario id"):
        load_scenarios(path)


def test_main_writes_the_artifact(tmp_path):
    destination = tmp_path / "memory_eval.json"

    code = main(["--scenarios", str(SCENARIOS), "--json", str(destination)])

    assert code == 0
    report = json.loads(destination.read_text())
    assert report["scenario_count"] >= 20
    assert report["by_category"]["status"]["scenarios"] >= 5
    assert report["update_blind_baseline"]["accuracy_current"] < 0.5
