#!/usr/bin/env python3
"""Cost and latency per model tier.

Phase 3 — Person A.

The plan's third number: what each Nemotron tier costs, and how fast it
is, across a representative set of calls. It is the number that makes the
tiering decision defensible rather than intuitive — a Nano redaction pass
is only worth it if the Super call it protects is genuinely more expensive
than the redaction itself.

The benchmark runs the same set of representative prompts against each
tier and records:

* **cost** — from the token counts the API returns, priced at the tier's
  published rate. Token Factory returns usage on every response, so this
  is measured, not estimated.
* **latency** — wall clock per call, plus the p95 across repetitions,
  because the mean hides the tail and the tail is what a user feels.
* **tokens** — input and output, separately, because the two are priced
  very differently.

The representative set is not synthetic filler. It is drawn from the
pipeline: a redaction request, a deadline extraction request, an action
draft, and a memory update. Those are the four things the agent actually
asks a model to do.

Pricing is not hard-coded here. It is read from `eval/pricing.json`, which
records the catalog snapshot the rates came from. The script refuses to run
without a complete rate entry for every tier; a benchmark that silently
uses stale or missing prices produces a confident wrong number.

The TierResult objects are intentionally independent of the transport.
`run_bench()` accepts a callable transport, so the test suite runs the
whole scheduling, accounting, and error path with no network. The default
transport is the only thing that knows how to talk to Token Factory.

Usage:

    python eval/cost_latency_bench.py --repeats 3
    python eval/cost_latency_bench.py --pricing eval/pricing.json

`NEBIUS_API_KEY` is read from the environment or `.env`, through the same
loader the other eval scripts use. The key is never printed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.tokenfactory import base_url, load_api_key

__all__ = [
    "DEFAULT_PROMPTS",
    "DEFAULT_TIERS",
    "ChatTransport",
    "TierResult",
    "TokenFactoryTransport",
    "load_pricing",
    "main",
    "render_markdown",
    "run_bench",
]

EVAL_DIR = Path(__file__).resolve().parent
PRICING_PATH = EVAL_DIR / "pricing.json"
RESULTS_DIR = EVAL_DIR / "results"

# The three pinned tiers from docs/model-catalog.md and eval/pin_models.py.
# Super is Super-VL because that is the pinned main-reasoning and OCR model;
# the text-only `nemotron-3-super-120b-a12b` is a comparator, not the tier.
DEFAULT_TIERS = [
    ("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "Nano"),
    ("nvidia/Nemotron-3.5-Super-VL", "Super"),
    ("nvidia/Nemotron-3-Ultra-550b-a55b", "Ultra"),
]

# Four representative pipeline calls. The prompts are synthetic, short, and
# deliberately contain no real household data. They are not a quality
# benchmark; they are the shape of the work the agent sends to a model.
DEFAULT_PROMPTS: list[dict[str, Any]] = [
    {
        "id": "redaction",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a deterministic privacy filter. Return only a JSON "
                    "object with a `redacted` string and a `spans` array. Do not "
                    "explain."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Redact every personal name, account number, email address, "
                    "and postal address. Keep dates and merchant names. Text: "
                    "'On 2 November 2026, Rowan Example (account "
                    "ACCT-EXAMPLE-0001, rowan@example.invalid) renewed the "
                    "Northwind Insurance policy from 14 Example Street.'"
                ),
            },
        ],
    },
    {
        "id": "deadline_extraction",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract structured fields. Return only a JSON object with "
                    "provider, holder, reference, renewal_date, and amount. Use "
                    "ISO dates and a number for amount."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Northwind Insurance renewal notice for Rowan Example. "
                    "Policy POL-EXAMPLE-42 renews on 2 November 2026. The "
                    "annual amount is 148.00 USD."
                ),
            },
        ],
    },
    {
        "id": "action_draft",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Draft a concise, polite message the user can send. Do not "
                    "invent account details. Return only the message body."
                ),
            },
            {
                "role": "user",
                "content": (
                    "The household cancelled the Example Gym membership. Draft "
                    "a cancellation confirmation request for the gym, effective "
                    "at the end of the current term. Ask for written "
                    "confirmation and the final billing date."
                ),
            },
        ],
    },
    {
        "id": "memory_update",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Update a household fact. Return only a JSON object with "
                    "status, effective_date, and supersedes."
                ),
            },
            {
                "role": "user",
                "content": (
                    "The user says: 'I cancelled the Northwind policy in March.' "
                    "Existing fact: Northwind policy POL-EXAMPLE-42 is active, "
                    "renews 2 November 2026. Record the new status and the month "
                    "it took effect."
                ),
            },
        ],
    },
]

MAX_COMPLETION_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TIMEOUT_SECONDS = 120.0


class ChatTransport(Protocol):
    """One chat-completion call.

    The transport returns a mapping containing token usage. The default
    adapter normalises Token Factory's OpenAI-compatible response; tests
    inject a fake with the same shape.
    """

    def __call__(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Mapping[str, Any]: ...


@dataclass
class TierResult:
    """The measured cost and latency of one tier."""

    model: str
    tier: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    error_types: list[str] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return self.calls + self.errors

    @property
    def mean_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def min_latency(self) -> float:
        return min(self.latencies) if self.latencies else 0.0

    @property
    def max_latency(self) -> float:
        return max(self.latencies) if self.latencies else 0.0

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        return ordered[max(0, int(len(ordered) * 0.95) - 1)]

    def cost_usd(self, pricing: Mapping[str, Any]) -> float:
        """Cost in USD using the supplied per-million-token rates."""
        rates = pricing.get(self.tier) or pricing.get(self.model) or {}
        if not isinstance(rates, Mapping):
            return 0.0
        per_million_in = float(rates.get("input_per_million", 0.0))
        per_million_out = float(rates.get("output_per_million", 0.0))
        return (
            self.prompt_tokens / 1_000_000 * per_million_in
            + self.completion_tokens / 1_000_000 * per_million_out
        )

    def cost_per_call_usd(self, pricing: Mapping[str, Any]) -> float:
        if not self.calls:
            return 0.0
        return self.cost_usd(pricing) / self.calls

    def to_dict(self, pricing: Mapping[str, Any]) -> dict[str, Any]:
        """The JSON-safe record for one tier. No prompt text is included."""
        return {
            "tier": self.tier,
            "model": self.model,
            "attempts": self.attempts,
            "calls": self.calls,
            "errors": self.errors,
            "error_types": sorted(set(self.error_types)),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "mean_latency_seconds": round(self.mean_latency, 6),
            "min_latency_seconds": round(self.min_latency, 6),
            "p50_latency_seconds": round(self.p50_latency, 6),
            "p95_latency_seconds": round(self.p95_latency, 6),
            "max_latency_seconds": round(self.max_latency, 6),
            "cost_usd": round(self.cost_usd(pricing), 8),
            "cost_per_call_usd": round(self.cost_per_call_usd(pricing), 8),
        }


class TokenFactoryTransport:
    """The default transport: Token Factory's OpenAI-compatible endpoint.

    A single `httpx.Client` is reused for the whole run so connection setup
    is not counted as model latency. The client is closed by `close()`.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required for the default transport")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        import httpx

        self._api_key = api_key
        self._url = f"{(base or base_url()).rstrip('/')}/chat/completions"
        self._client = httpx.Client(timeout=timeout)

    def __call__(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Mapping[str, Any]:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        response = self._client.post(
            self._url,
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if response.status_code != 200:
            # Do not put the response body in the error: it can echo the
            # prompt, and this script's output is committed evidence.
            raise RuntimeError(f"HTTP {response.status_code} from Token Factory")

        data = response.json()
        usage = data.get("usage")
        if not isinstance(usage, Mapping):
            raise RuntimeError("response omitted token usage")
        if usage.get("prompt_tokens") is None or usage.get("completion_tokens") is None:
            raise RuntimeError("response omitted prompt/completion token counts")
        return {
            "prompt_tokens": int(usage["prompt_tokens"]),
            "completion_tokens": int(usage["completion_tokens"]),
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TokenFactoryTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _usage_tokens(response: Mapping[str, Any]) -> tuple[int, int]:
    """Read token usage from a transport response.

    Accepts both the normalised shape and a nested OpenAI-style `usage`
    object, so a fake transport can mirror whichever is convenient.
    """
    usage: Any = response.get("usage", response)
    if not isinstance(usage, Mapping):
        raise RuntimeError("transport response has no token usage")
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if prompt is None or completion is None:
        raise RuntimeError("transport response omitted prompt/completion tokens")
    try:
        prompt_tokens = int(prompt)
        completion_tokens = int(completion)
    except (TypeError, ValueError):
        raise RuntimeError("transport response had non-integer token usage") from None
    if prompt_tokens < 0 or completion_tokens < 0:
        raise RuntimeError("transport response had negative token usage")
    return prompt_tokens, completion_tokens


def _prompt_id(prompt: Mapping[str, Any], index: int) -> str:
    return str(prompt.get("id") or f"prompt-{index + 1}")


def _messages_for(prompt: Mapping[str, Any], prompt_id: str) -> list[dict[str, str]]:
    raw = prompt.get("messages")
    if raw is None:
        if "role" in prompt and "content" in prompt:
            raw = [prompt]
        else:
            raw = []
            if prompt.get("system"):
                raw.append({"role": "system", "content": prompt["system"]})
            if prompt.get("user"):
                raw.append({"role": "user", "content": prompt["user"]})

    if not isinstance(raw, list) or not raw:
        raise ValueError(f"prompt {prompt_id!r} has no messages")

    messages: list[dict[str, str]] = []
    for message in raw:
        if not isinstance(message, Mapping):
            raise ValueError(f"prompt {prompt_id!r} has a non-object message")
        if "role" not in message or "content" not in message:
            raise ValueError(f"prompt {prompt_id!r} message needs role and content")
        messages.append(
            {"role": str(message["role"]), "content": str(message["content"])}
        )
    return messages


def _positive_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return number


def _temperature_for(prompt: Mapping[str, Any], prompt_id: str) -> float:
    value = prompt.get("temperature", DEFAULT_TEMPERATURE)
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"prompt {prompt_id!r} has a non-numeric temperature") from None
    if not 0.0 <= temperature <= 2.0:
        raise ValueError(f"prompt {prompt_id!r} temperature must be between 0 and 2")
    return temperature


def _prepare_prompts(
    prompts: list[dict[str, Any]], max_tokens: int
) -> list[tuple[str, list[dict[str, str]], int, float]]:
    prepared = []
    for index, prompt in enumerate(prompts):
        if not isinstance(prompt, Mapping):
            raise ValueError(f"prompt {index + 1} is not an object")
        prompt_id = _prompt_id(prompt, index)
        messages = _messages_for(prompt, prompt_id)
        prompt_max_tokens = _positive_int(prompt.get("max_tokens", max_tokens), "max_tokens")
        prepared.append(
            (
                prompt_id,
                messages,
                prompt_max_tokens,
                _temperature_for(prompt, prompt_id),
            )
        )
    return prepared


def load_pricing(
    path: str | Path = PRICING_PATH,
    tiers: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Read and validate the per-tier rate table.

    A missing file, malformed JSON, or a missing/negative rate is an error.
    The benchmark must not silently use zero for a tier it cannot price.
    """
    pricing_path = Path(path)
    try:
        text = pricing_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read pricing file {pricing_path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pricing file {pricing_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"pricing file {pricing_path} must contain a JSON object")

    for model, tier in tiers or DEFAULT_TIERS:
        rates = data.get(tier) or data.get(model)
        if not isinstance(rates, Mapping):
            raise ValueError(f"pricing is missing a rate entry for {tier!r} ({model})")
        for key in ("input_per_million", "output_per_million"):
            value = rates.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"pricing for {tier!r} is missing a numeric {key}")
            if value < 0:
                raise ValueError(f"pricing for {tier!r} has a negative {key}")
    return dict(data)


def run_bench(
    tiers: list[tuple[str, str]],
    prompts: list[dict[str, Any]],
    *,
    repeats: int = 3,
    api_key: str = "",
    transport: ChatTransport | None = None,
    max_tokens: int = MAX_COMPLETION_TOKENS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[TierResult]:
    """Run the benchmark.

    Args:
        tiers: (model_id, tier_name) pairs.
        prompts: Representative prompts.
        repeats: How many times to run each prompt against each tier, so
            the latency distribution means something.
        api_key: The Token Factory API key. Only used when `transport` is
            omitted.
        transport: Injected callable for offline tests and alternate
            endpoints.
        max_tokens: Default completion budget. Individual prompts may
            override it.
        timeout: Per-request timeout for the default transport.

    Returns:
        One `TierResult` per tier, in the order supplied.

    Raises:
        ValueError: Empty/duplicate tiers, no prompts, a bad repetition
            count, an invalid prompt, or no API key with no transport.
    """
    repeats = _positive_int(repeats, "repeats")
    max_tokens = _positive_int(max_tokens, "max_tokens")
    if not tiers:
        raise ValueError("at least one tier is required")
    if not prompts:
        raise ValueError("at least one prompt is required")

    seen: set[tuple[str, str]] = set()
    for model, tier in tiers:
        if not model or not tier:
            raise ValueError("tier model and name must be non-empty")
        key = (model, tier)
        if key in seen:
            raise ValueError(f"duplicate tier entry: {tier!r} ({model})")
        seen.add(key)

    prepared = _prepare_prompts(prompts, max_tokens)
    results = [TierResult(model=model, tier=tier) for model, tier in tiers]
    by_key = {(result.model, result.tier): result for result in results}

    owned_transport: TokenFactoryTransport | None = None
    if transport is None:
        if not api_key:
            raise ValueError("api_key is required when no transport is injected")
        owned_transport = TokenFactoryTransport(api_key, timeout=timeout)
        transport = owned_transport

    try:
        # Interleave tiers inside each prompt/repetition. Running one tier
        # to completion first would let a network change or a warm cache
        # bias the comparison toward whichever tier went first.
        for _ in range(repeats):
            for _prompt_id, messages, prompt_max_tokens, temperature in prepared:
                for model, tier in tiers:
                    result = by_key[(model, tier)]
                    started = time.perf_counter()
                    try:
                        response = transport(
                            model,
                            messages,
                            max_tokens=prompt_max_tokens,
                            temperature=temperature,
                        )
                        elapsed = time.perf_counter() - started
                        prompt_tokens, completion_tokens = _usage_tokens(response)
                    except Exception as exc:
                        # The exception text is not recorded: a transport
                        # may include request/response text in it.
                        result.errors += 1
                        result.error_types.append(type(exc).__name__)
                        continue

                    result.calls += 1
                    result.prompt_tokens += prompt_tokens
                    result.completion_tokens += completion_tokens
                    result.latencies.append(elapsed)
    finally:
        if owned_transport is not None:
            owned_transport.close()

    return results


def render_markdown(
    results: list[TierResult],
    pricing: Mapping[str, Any],
    *,
    repeats: int,
    prompts: list[dict[str, Any]],
) -> str:
    """Render the video-ready table and its method note."""
    meta = pricing.get("_meta")
    source = "eval/pricing.json"
    snapshot = ""
    if isinstance(meta, Mapping):
        source = str(meta.get("source") or source)
        snapshot = str(meta.get("snapshot") or "")

    lines = [
        "# Cost and latency by Nemotron tier",
        "",
        f"Generated by `eval/cost_latency_bench.py` · repeats per prompt: {repeats} · "
        f"prompts: {len(prompts)}",
        "",
        f"Pricing: {source}" + (f" (`{snapshot}`)" if snapshot else ""),
        "",
        "Latency is wall-clock around one chat completion, including network "
        "time. Cost uses the token usage returned by the API and the rates in "
        "`eval/pricing.json`. Errors are counted but their messages are not "
        "recorded.",
        "",
        "| Tier | Model | Calls | Errors | Mean | p50 | p95 | Tokens in | Tokens out | Cost | $/call |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.tier} | `{result.model}` | {result.calls} | "
            f"{result.errors} | {result.mean_latency:.3f}s | "
            f"{result.p50_latency:.3f}s | {result.p95_latency:.3f}s | "
            f"{result.prompt_tokens:,} | {result.completion_tokens:,} | "
            f"${result.cost_usd(pricing):.6f} | "
            f"${result.cost_per_call_usd(pricing):.6f} |"
        )
    lines += [
        "",
        "## Prompts",
        "",
    ]
    for index, prompt in enumerate(prompts):
        prompt_id = _prompt_id(prompt, index)
        lines.append(f"- `{prompt_id}`")
    return "\n".join(lines) + "\n"


def _result_payload(
    results: list[TierResult],
    pricing: Mapping[str, Any],
    *,
    repeats: int,
    prompts: list[dict[str, Any]],
) -> dict[str, Any]:
    meta = pricing.get("_meta")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repeats": repeats,
        "prompts": [_prompt_id(prompt, index) for index, prompt in enumerate(prompts)],
        "pricing": dict(meta) if isinstance(meta, Mapping) else {},
        "tiers": [result.to_dict(pricing) for result in results],
    }


def main(argv: list[str] | None = None, *, transport: ChatTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--json",
        dest="json_out",
        default=str(RESULTS_DIR / "cost_latency.json"),
        help="JSON result path",
    )
    parser.add_argument(
        "--markdown",
        dest="markdown_out",
        default=str(RESULTS_DIR / "cost_latency.md"),
        help="Markdown table path",
    )
    parser.add_argument(
        "--pricing",
        default=str(PRICING_PATH),
        help="per-tier rate table",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_COMPLETION_TOKENS,
        help="default completion budget",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="per-request timeout for the default transport",
    )
    args = parser.parse_args(argv)

    try:
        pricing = load_pricing(args.pricing, DEFAULT_TIERS)
    except ValueError as exc:
        print(f"pricing error: {exc}", file=sys.stderr)
        return 1

    api_key = ""
    if transport is None:
        api_key = load_api_key()
        if not api_key:
            print(
                "NEBIUS_API_KEY is not set (environment or .env); refusing to "
                "run a live benchmark without credentials.",
                file=sys.stderr,
            )
            return 1

    try:
        results = run_bench(
            DEFAULT_TIERS,
            DEFAULT_PROMPTS,
            repeats=args.repeats,
            api_key=api_key,
            transport=transport,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
    except ValueError as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"benchmark failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    markdown = render_markdown(
        results,
        pricing,
        repeats=args.repeats,
        prompts=DEFAULT_PROMPTS,
    )
    payload = _result_payload(
        results,
        pricing,
        repeats=args.repeats,
        prompts=DEFAULT_PROMPTS,
    )

    json_path = Path(args.json_out)
    markdown_path = Path(args.markdown_out)
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        markdown_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        print(f"could not write results ({type(exc).__name__})", file=sys.stderr)
        return 1

    print(markdown)
    print(f"wrote {json_path} and {markdown_path}")

    if any(result.calls == 0 for result in results):
        print(
            "one or more tiers produced no successful calls; inspect the "
            "error counts before using this table",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
