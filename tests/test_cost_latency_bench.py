"""Tests for the Phase 3 cost/latency benchmark.

The benchmark's value depends on two properties that are easy to lose:

* **model IDs come from the catalog** — a test that asserts the resolution
  path will not save a hard-coded ID from creeping back, but it will catch
  the day a pin goes stale or a role disappears;
* **a missing price is an error** — a zero-rate row silent in the table is
  a confident wrong number in the video.

The measurement itself is tested with a fake transport, so the suite never
spends money or needs a network.
"""

from __future__ import annotations

import pytest

from eval import cost_latency_bench as bench


def _model(
    model_id: str,
    *,
    modality: str = "text->text",
    prompt_price: float = 0.0000003,
    completion_price: float = 0.0000009,
) -> dict:
    return {
        "id": model_id,
        "architecture": {"modality": modality},
        "pricing": {"prompt": str(prompt_price), "completion": str(completion_price)},
    }


CATALOG = [
    _model("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"),
    _model("nvidia/Nemotron-3.5-Super-VL", modality="text+image->text"),
    _model("nvidia/nemotron-3-super-120b-a12b"),
    _model("nvidia/Nemotron-3-Ultra-550b-a55b", prompt_price=0.000001, completion_price=0.000003),
]


def test_resolution_prefers_the_pinned_role():
    tiers = bench.resolve_tiers(
        CATALOG,
        pinned={"nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "super": "nvidia/Nemotron-3.5-Super-VL"},
    )

    assert [tier.role for tier in tiers] == ["nano", "super", "ultra"]
    assert tiers[1].model == "nvidia/Nemotron-3.5-Super-VL"
    assert tiers[2].input_per_million == 1.0
    assert tiers[2].output_per_million == 3.0


def test_resolution_falls_back_to_the_catalog_and_prefers_text():
    tiers = bench.resolve_tiers(CATALOG, pinned={})

    # "super" matches both the VL model and the text model; the bench sends
    # text, so the text-only record wins.
    assert tiers[1].model == "nvidia/nemotron-3-super-120b-a12b"


def test_resolution_ignores_a_stale_pin():
    tiers = bench.resolve_tiers(CATALOG, pinned={"super": "nvidia/removed-model"})

    assert tiers[1].model == "nvidia/nemotron-3-super-120b-a12b"


def test_unpriced_model_is_an_error_not_a_zero_row():
    catalog = [_model("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", prompt_price=0, completion_price=0)]

    with pytest.raises(RuntimeError, match="no price"):
        bench.resolve_tiers(catalog, pinned={})


def test_missing_role_is_an_error():
    with pytest.raises(RuntimeError, match="no catalog model"):
        bench.resolve_tiers(CATALOG[:1], pinned={"ultra": "nvidia/removed-model"})


def test_p95_is_nearest_rank_not_interpolated():
    samples = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.99]

    # ceil(0.95 * 12) - 1 == 11 — the slowest call, which is the point.
    assert bench.p95(samples) == 0.99
    assert bench.p95([]) == 0.0


def test_run_bench_aggregates_tokens_and_latency(monkeypatch):
    calls: list[str] = []

    def fake_chat(model, prompt, *, api_key, timeout, max_tokens):
        calls.append(prompt.prompt_id)
        return 100, 50, 0.25

    monkeypatch.setattr(bench, "_chat", fake_chat)
    tiers = bench.resolve_tiers(CATALOG)

    results = bench.run_bench(
        tiers[:2],
        bench.prompts(),
        repeats=2,
        api_key="test",
    )

    assert calls.count("transcribe") == 4  # two tiers x two repeats
    for tier in results:
        assert tier.calls == 8
        assert tier.errors == 0
        assert tier.prompt_tokens == 800
        assert tier.completion_tokens == 400
        assert tier.mean_latency == pytest.approx(0.25)
        assert tier.cost_usd > 0
        assert set(tier.by_prompt) == {"transcribe", "deadline_extract", "action_draft", "memory_update"}


def test_run_bench_records_a_failed_call_instead_of_dropping_it(monkeypatch):
    def broken_chat(model, prompt, *, api_key, timeout, max_tokens):
        raise RuntimeError("HTTP 503: overloaded")

    monkeypatch.setattr(bench, "_chat", broken_chat)
    results = bench.run_bench(bench.resolve_tiers(CATALOG)[:1], bench.prompts(), repeats=1)

    assert results[0].calls == 0
    assert results[0].errors == 4
    assert "HTTP 503" in results[0].error_messages[0]
    assert results[0].cost_usd == 0


def test_run_bench_retries_a_transient_failure(monkeypatch):
    attempts = {"count": 0}

    def flaky_chat(model, prompt, *, api_key, timeout, max_tokens):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("[SSL: UNEXPECTED_EOF_WHILE_READING]")
        return 10, 5, 0.1

    monkeypatch.setattr(bench, "_chat", flaky_chat)
    results = bench.run_bench(
        bench.resolve_tiers(CATALOG)[:1],
        bench.prompts()[:1],
        repeats=1,
        api_key="test",
        retries=1,
    )

    assert results[0].calls == 1
    assert results[0].retries == 1
    assert results[0].errors == 0


def test_run_bench_does_not_retry_a_client_error(monkeypatch):
    def refused_chat(model, prompt, *, api_key, timeout, max_tokens):
        raise RuntimeError("HTTP 400: bad request")

    monkeypatch.setattr(bench, "_chat", refused_chat)
    results = bench.run_bench(
        bench.resolve_tiers(CATALOG)[:1],
        bench.prompts()[:1],
        repeats=1,
        api_key="test",
        retries=1,
    )

    assert results[0].calls == 0
    assert results[0].retries == 0
    assert results[0].errors == 1


def test_prompts_carry_placeholders_not_personal_details():
    text = " ".join(p.system + p.user for p in bench.prompts())

    # A benchmark is not a reason to put a name, an email, or a real
    # reference into a prompt.
    assert "[PROVIDER_1]" in text
    assert "[ACCOUNT_1]" in text
    assert "@" not in text
