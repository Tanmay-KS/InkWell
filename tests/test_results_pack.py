"""Tests for the Phase 3 results packer.

The packer is the only path from an artifact to a chart, so the tests are
about the two ways it could mislead:

* publishing a smoke run as if it were a measurement;
* copying a synthetic personal detail out of an artifact and into a page.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval import results_pack


def _leak(documents: int = 200) -> dict:
    off = {
        "condition": "router_off",
        "headline": {"payloads": documents, "payloads_with_pii": documents, "leak_rate": 1.0},
    }
    on = {
        "condition": "router_on",
        "headline": {"payloads": documents, "payloads_with_pii": 0, "leak_rate": 0.0},
    }
    return {
        "documents": documents,
        "reference_date": "2026-10-20",
        "conditions": {"router_off": off, "router_on": on},
        # The worst case for the packer: a real-looking detail in the artifact.
        "examples": [{"excerpt": "Lakeshore ... 0847-4851 Devin Sullivan"}],
    }


def _memory(scenarios: int = 20) -> dict:
    scores = {
        "accuracy_current": 0.9,
        "accuracy_superseded": 0.8,
        "accuracy_never_stated": 1.0,
        "accuracy_overall": 0.9,
    }
    return {
        "method": "deterministic SQLite fact supersession; no model call",
        "scenario_count": scenarios,
        "scores": scores,
        "update_blind_baseline": {
            "accuracy_current": 0.0,
            "accuracy_superseded": 1.0,
            "accuracy_never_stated": 1.0,
            "accuracy_overall": 0.66,
        },
    }


def _auditor(planted: int = 5) -> dict:
    return {
        "measured_at": "2026-10-29T12:00:00+00:00",
        "synthetic": True,
        "method": "deterministic Auditor over the real orchestrator",
        "planted_count": planted,
        "rejected_first_pass": planted,
        "catch_rate": 1.0,
        "false_approvals": 0,
        "false_rejects": 0,
        "approved_after_revision": planted,
        "two_rounds_in_trace": planted,
        "cases": [
            {
                "case_id": "case-01-wrong-amount",
                "description": "Draft states a wrong amount.",
                "first_decision": "reject",
                "final_decision": "approve",
                "first_codes": ["amount_mismatch"],
            }
        ],
    }


def _cost(calls: int = 20) -> dict:
    return {
        "measured_at": "2026-10-28T17:00:00+00:00",
        "endpoint": "https://api.tokenfactory.nebius.com/v1",
        "repeats": 5,
        "catalog_models": 30,
        "total_cost_usd": 0.01,
        "tiers": [
            {
                "tier": tier,
                "model": f"nvidia/{tier}-model",
                "calls": calls,
                "errors": 0,
                "mean_latency_ms": 1000,
                "p95_latency_ms": 2000,
                "cost_per_call_usd": 0.0001,
                "cost_per_1k_calls_usd": 0.1,
            }
            for tier in ("Nano", "Super", "Ultra")
        ],
    }


def test_a_smoke_run_is_not_reportable(tmp_path):
    _, finding = results_pack.load_leak(tmp_path / "leak.json")

    assert finding.ready is False
    assert "missing artifact" in finding.detail


def test_leak_needs_the_canonical_document_count():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "leak.json"
        path.write_text(json.dumps(_leak(5)))

        _, finding = results_pack.load_leak(path)

    assert finding.ready is False
    assert "5 documents" in finding.detail


def test_memory_needs_enough_scenarios():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "memory.json"
        path.write_text(json.dumps(_memory(3)))

        _, finding = results_pack.load_memory(path)

    assert finding.ready is False
    assert "3 scenarios" in finding.detail


def test_auditor_needs_the_planted_cases():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "auditor.json"
        path.write_text(json.dumps(_auditor(planted=1)))

        _, finding = results_pack.load_auditor(path)

    assert finding.ready is False
    assert "1 planted cases" in finding.detail


def test_cost_needs_successful_calls():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cost.json"
        path.write_text(json.dumps(_cost(calls=0)))

        _, finding = results_pack.load_cost(path)

    assert finding.ready is False
    assert "0 successful calls" in finding.detail


def test_report_never_copies_an_example_excerpt():
    leak = _leak()
    _, leak_finding = results_pack.load_leak(Path("leak.json"))
    report = results_pack.render_report(
        [
            leak_finding,
            results_pack.load_memory(Path("memory.json"))[1],
            results_pack.load_cost(Path("cost.json"))[1],
            results_pack.load_auditor(Path("auditor.json"))[1],
        ],
        leak,
        _memory(),
        _cost(),
        _auditor(),
        charts={},
        artifacts={},
    )

    assert "Devin" not in report
    assert "0847-4851" not in report


def test_a_chart_needs_one_value_per_category():
    try:
        results_pack.bar_chart(
            categories=["a", "b"],
            series=[("s", [1.0], "#000000")],
            title="broken",
        )
    except ValueError as exc:
        assert "one value per category" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a mis-sized series must not produce a chart")


def test_main_writes_the_report_and_charts(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "leak_test.json").write_text(json.dumps(_leak()))
    (results / "memory_eval.json").write_text(json.dumps(_memory()))
    (results / "cost_latency.json").write_text(json.dumps(_cost()))
    (results / "auditor_eval.json").write_text(json.dumps(_auditor()))

    docs = tmp_path / "docs"
    report = docs / "results.md"

    code = results_pack.main(
        [
            "--results",
            str(results),
            "--docs",
            str(docs),
            "--report",
            str(report),
        ]
    )

    assert code == 0
    assert report.exists()
    assert (docs / "results" / "leak-rate.svg").exists()
    assert (docs / "results" / "cost-per-1k.svg").exists()
    assert (docs / "results" / "latency-p95.svg").exists()
    assert (docs / "results" / "memory-accuracy.svg").exists()
    assert "**READY**" in report.read_text()


def test_main_returns_2_when_one_number_is_not_ready(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "leak_test.json").write_text(json.dumps(_leak()))
    (results / "memory_eval.json").write_text(json.dumps(_memory(3)))
    (results / "cost_latency.json").write_text(json.dumps(_cost()))
    (results / "auditor_eval.json").write_text(json.dumps(_auditor()))

    docs = tmp_path / "docs"
    code = results_pack.main(
        ["--results", str(results), "--docs", str(docs), "--report", str(docs / "results.md")]
    )

    assert code == 2
    assert "NOT READY" in (docs / "results.md").read_text()
