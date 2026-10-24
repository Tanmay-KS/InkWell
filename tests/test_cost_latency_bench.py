"""Offline tests for the Phase 3 cost/latency benchmark.

The benchmark's useful property is not that it can call Nebius. It is that
the scheduling, token accounting, error handling, pricing validation, and
evidence output are correct before a live key is spent on them. These tests
inject a transport and never touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.cost_latency_bench import (
    DEFAULT_PROMPTS,
    DEFAULT_TIERS,
    PRICING_PATH,
    TierResult,
    TokenFactoryTransport,
    load_pricing,
    main,
    render_markdown,
    run_bench,
)


def pricing_data() -> dict[str, object]:
    return {
        "_meta": {
            "source": "test catalog",
            "snapshot": "test-snapshot",
            "taken": "2026-10-22T00:00:00Z",
            "unit": "USD per 1,000,000 tokens",
        },
        "Nano": {"input_per_million": 0.06, "output_per_million": 0.24},
        "Super": {"input_per_million": 0.30, "output_per_million": 0.90},
        "Ultra": {"input_per_million": 1.00, "output_per_million": 3.00},
    }


def write_pricing(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class FakeTransport:
    """A deterministic transport that records calls and can fail by model."""

    def __init__(
        self,
        *,
        prompt_tokens: int = 100,
        completion_tokens: int = 25,
        fail_models: set[str] | None = None,
        fail_with: str = "synthetic transport failure",
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.fail_models = fail_models or set()
        self.fail_with = fail_with
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, int]:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if model in self.fail_models:
            raise RuntimeError(self.fail_with)
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def one_prompt() -> list[dict[str, object]]:
    return [
        {
            "id": "p1",
            "messages": [{"role": "user", "content": "a representative prompt"}],
        }
    ]


def test_run_bench_accounts_tokens_calls_and_latency():
    transport = FakeTransport(prompt_tokens=100, completion_tokens=25)
    tiers = [("model-a", "Nano"), ("model-b", "Super")]

    results = run_bench(
        tiers,
        one_prompt(),
        repeats=2,
        transport=transport,
    )

    assert [result.calls for result in results] == [2, 2]
    assert [result.attempts for result in results] == [2, 2]
    assert results[0].prompt_tokens == 200
    assert results[0].completion_tokens == 50
    assert len(results[0].latencies) == 2
    assert all(latency >= 0 for result in results for latency in result.latencies)
    assert transport.calls[0]["max_tokens"] == 4096
    assert transport.calls[0]["temperature"] == 0.0


def test_run_bench_interleaves_tiers_within_each_prompt_repetition():
    transport = FakeTransport()
    tiers = [("model-a", "Nano"), ("model-b", "Super")]
    prompts = [
        {
            "id": "p1",
            "messages": [{"role": "user", "content": "p1"}],
        },
        {
            "id": "p2",
            "messages": [{"role": "user", "content": "p2"}],
        },
    ]

    run_bench(tiers, prompts, repeats=1, transport=transport)

    observed = [
        (call["model"], call["messages"][-1]["content"])  # type: ignore[index]
        for call in transport.calls
    ]
    assert observed == [
        ("model-a", "p1"),
        ("model-b", "p1"),
        ("model-a", "p2"),
        ("model-b", "p2"),
    ]


def test_run_bench_records_error_types_without_messages():
    secret = "PROMPT-CONTENT-THAT-MUST-NOT-BE-LOGGED"
    transport = FakeTransport(fail_models={"bad-model"}, fail_with=secret)
    tiers = [("bad-model", "Nano"), ("good-model", "Super")]
    prompts = [
        {
            "id": "p1",
            "messages": [{"role": "user", "content": secret}],
        }
    ]

    results = run_bench(tiers, prompts, repeats=2, transport=transport)

    assert results[0].calls == 0
    assert results[0].errors == 2
    assert results[0].error_types == ["RuntimeError", "RuntimeError"]
    assert results[1].calls == 2
    assert results[1].errors == 0

    rendered = results[0].to_dict(pricing_data())
    assert "RuntimeError" in json.dumps(rendered)
    assert secret not in json.dumps(rendered)


def test_run_bench_reads_nested_usage_and_token_aliases():
    class NestedTransport:
        def __call__(self, model, messages, *, max_tokens, temperature):
            return {"usage": {"input_tokens": 11, "output_tokens": 7}}

    [result] = run_bench(
        [("model-a", "Nano")],
        one_prompt(),
        repeats=1,
        transport=NestedTransport(),
    )

    assert result.calls == 1
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7


def test_run_bench_uses_a_prompt_specific_max_tokens():
    transport = FakeTransport()
    prompts = [
        {
            "id": "p1",
            "max_tokens": 123,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ]

    run_bench([("model-a", "Nano")], prompts, repeats=1, transport=transport)

    assert transport.calls[0]["max_tokens"] == 123


def test_run_bench_accepts_system_and_user_shorthand():
    transport = FakeTransport()
    prompts = [{"id": "p1", "system": "be brief", "user": "hello"}]

    run_bench([("model-a", "Nano")], prompts, repeats=1, transport=transport)

    messages = transport.calls[0]["messages"]
    assert messages == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]


@pytest.mark.parametrize(
    ("tiers", "prompts", "kwargs"),
    [
        ([], one_prompt(), {}),
        ([("model-a", "Nano")], [], {}),
        ([("model-a", "Nano")], one_prompt(), {"repeats": 0}),
        (
            [("model-a", "Nano"), ("model-a", "Nano")],
            one_prompt(),
            {},
        ),
        ([("", "Nano")], one_prompt(), {}),
        ([("model-a", "Nano")], [{"id": "p1"}], {}),
        ([("model-a", "Nano")], one_prompt(), {"max_tokens": 0}),
    ],
)
def test_run_bench_rejects_bad_inputs(tiers, prompts, kwargs):
    with pytest.raises(ValueError):
        run_bench(tiers, prompts, transport=FakeTransport(), **kwargs)


def test_run_bench_requires_a_key_without_an_injected_transport():
    with pytest.raises(ValueError):
        run_bench([("model-a", "Nano")], one_prompt(), repeats=1)


def test_cost_usd_uses_per_million_rates():
    result = TierResult(
        model="model-a",
        tier="Nano",
        calls=2,
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
    )

    cost = result.cost_usd(pricing_data())

    assert cost == pytest.approx(0.06 + 0.12)
    assert result.cost_per_call_usd(pricing_data()) == pytest.approx(0.09)


def test_cost_usd_can_fall_back_to_a_model_key():
    result = TierResult(
        model="model-a",
        tier="Nano",
        calls=1,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    pricing = {"model-a": {"input_per_million": 2.0, "output_per_million": 4.0}}

    assert result.cost_usd(pricing) == pytest.approx(2.0)


def test_latency_statistics_include_the_tail():
    result = TierResult(
        model="model-a",
        tier="Nano",
        latencies=[1.0, 2.0, 3.0, 4.0, 5.0],
    )

    assert result.mean_latency == pytest.approx(3.0)
    assert result.min_latency == 1.0
    assert result.max_latency == 5.0
    assert result.p50_latency == 3.0
    assert result.p95_latency == 4.0


def test_load_pricing_accepts_the_committed_table():
    pricing = load_pricing(PRICING_PATH, DEFAULT_TIERS)

    assert pricing["Nano"]["input_per_million"] == pytest.approx(0.06)
    assert pricing["Super"]["output_per_million"] == pytest.approx(0.90)
    assert pricing["Ultra"]["input_per_million"] == pytest.approx(1.0)


def test_load_pricing_accepts_model_keyed_rates(tmp_path):
    path = write_pricing(
        tmp_path,
        {"model-a": {"input_per_million": 0.1, "output_per_million": 0.2}},
    )

    pricing = load_pricing(path, [("model-a", "Nano")])

    assert pricing["model-a"]["input_per_million"] == pytest.approx(0.1)


def test_load_pricing_rejects_a_missing_tier(tmp_path):
    path = write_pricing(tmp_path, {"Nano": pricing_data()["Nano"]})

    with pytest.raises(ValueError, match="missing"):
        load_pricing(path, [("model-a", "Nano"), ("model-b", "Super")])


def test_load_pricing_rejects_a_negative_rate(tmp_path):
    path = write_pricing(
        tmp_path,
        {"Nano": {"input_per_million": -1, "output_per_million": 0}},
    )

    with pytest.raises(ValueError, match="negative"):
        load_pricing(path, [("model-a", "Nano")])


def test_load_pricing_rejects_malformed_json(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        load_pricing(path)


def test_render_markdown_contains_the_table_and_only_prompt_ids():
    result = TierResult(
        model="model-a",
        tier="Nano",
        calls=1,
        prompt_tokens=100,
        completion_tokens=20,
        latencies=[0.5],
    )
    prompts = [
        {
            "id": "redaction",
            "messages": [{"role": "user", "content": "PROMPT-TEXT"}],
        }
    ]

    markdown = render_markdown(
        [result],
        pricing_data(),
        repeats=1,
        prompts=prompts,
    )

    assert "| Tier | Model | Calls | Errors | Mean | p50 | p95 |" in markdown
    assert "`model-a`" in markdown
    assert "`redaction`" in markdown
    assert "PROMPT-TEXT" not in markdown


def test_main_without_api_key_returns_one_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    pricing = write_pricing(tmp_path, pricing_data())
    json_out = tmp_path / "out.json"
    markdown_out = tmp_path / "out.md"

    code = main(
        [
            "--pricing",
            str(pricing),
            "--json",
            str(json_out),
            "--markdown",
            str(markdown_out),
        ]
    )

    assert code == 1
    assert not json_out.exists()
    assert not markdown_out.exists()
    assert "NEBIUS_API_KEY" in capsys.readouterr().err


def test_main_writes_json_and_markdown_with_an_injected_transport(tmp_path):
    pricing = write_pricing(tmp_path, pricing_data())
    json_out = tmp_path / "out.json"
    markdown_out = tmp_path / "out.md"
    transport = FakeTransport(prompt_tokens=120, completion_tokens=30)

    code = main(
        [
            "--repeats",
            "1",
            "--pricing",
            str(pricing),
            "--json",
            str(json_out),
            "--markdown",
            str(markdown_out),
        ],
        transport=transport,
    )

    assert code == 0
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["repeats"] == 1
    assert len(payload["tiers"]) == len(DEFAULT_TIERS)
    assert {tier["tier"] for tier in payload["tiers"]} == {
        tier for _, tier in DEFAULT_TIERS
    }
    assert all(tier["calls"] == len(DEFAULT_PROMPTS) for tier in payload["tiers"])
    assert "Cost and latency by Nemotron tier" in markdown_out.read_text(
        encoding="utf-8"
    )
    assert "Northwind Insurance" not in json_out.read_text(encoding="utf-8")


def test_main_returns_one_when_a_tier_has_no_successful_calls(tmp_path):
    pricing = write_pricing(tmp_path, pricing_data())
    transport = FakeTransport(fail_models={"nvidia/Nemotron-3-Ultra-550b-a55b"})

    code = main(
        [
            "--repeats",
            "1",
            "--pricing",
            str(pricing),
            "--json",
            str(tmp_path / "out.json"),
            "--markdown",
            str(tmp_path / "out.md"),
        ],
        transport=transport,
    )

    assert code == 1
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    ultra = next(tier for tier in payload["tiers"] if tier["tier"] == "Ultra")
    assert ultra["calls"] == 0
    assert ultra["errors"] == len(DEFAULT_PROMPTS)


def test_token_factory_transport_requires_a_key_and_positive_timeout():
    with pytest.raises(ValueError):
        TokenFactoryTransport("")
    with pytest.raises(ValueError):
        TokenFactoryTransport("key", timeout=0)
