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

* **cost** — from the token counts the API returns, priced at the rate the
  live catalog publishes. Token Factory returns `usage` on every response,
  so this is measured, not estimated.
* **latency** — wall clock per call, plus the p95 across repetitions,
  because the mean hides the tail and the tail is what a user feels.
* **tokens** — input and output, separately, because the two are priced
  very differently.

The representative set is drawn from the four request shapes the agent
sends when it escalates to a model: transcribe, extract a deadline, draft
an action, record a memory update. The deterministic path does most of the
work (`agent/pipeline.py` runs with no model call); this measures the
calls that remain.

Two rules this script will not bend:

1. **Model IDs come from the live catalog, never from this file.** The
   tier slots are role names (`nano`, `super`, `ultra`); the pinned role in
   `.env` wins when it still exists in the catalog, otherwise the catalog's
   own matcher decides. A hard-coded ID here would rot silently the next
   time Nebius re-spells a model.
2. **Pricing comes from the live catalog, and a missing price is an
   error.** A cost table built on stale or guessed rates is a confident
   wrong number, which is worse than no number.

Usage:

    python eval/cost_latency_bench.py --dry-run      # resolve, spend nothing
    python eval/cost_latency_bench.py                # measure, 3 repeats
    python eval/cost_latency_bench.py --repeats 5 --json eval/results/cost_latency.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.tokenfactory import (
    ROLE_PATTERNS,
    base_url,
    fetch_models,
    find_by_pattern,
    find_model,
    load_api_key,
    model_id,
    pricing_per_token,
    read_pinned_roles,
)

__all__ = [
    "Prompt",
    "Tier",
    "TierSpec",
    "main",
    "p95",
    "prompts",
    "resolve_tiers",
    "run_bench",
]

# The tier slots from the plan, in ascending order of cost.
TIER_ROLES = ("nano", "super", "ultra")

# Reasoning models spend completion tokens before they answer. A 1024-token
# budget is consumed by the trace and the answer comes back empty, which
# would look like a fast call and a cheap one. The pipeline uses the same
# budget for the OCR path for the same reason.
MAX_COMPLETION_TOKENS = 4096


@dataclass(slots=True)
class Prompt:
    """One representative request.

    Attributes:
        prompt_id: Stable name used in the results.
        system: The system message, if the shape uses one.
        user: The user message. Redacted text only — a benchmark is not a
            reason to send a personal detail to a model.
        why: What pipeline stage this stands in for, for the report.
    """

    prompt_id: str
    system: str
    user: str
    why: str


def prompts() -> list[Prompt]:
    """The four request shapes, with placeholder text only.

    The numbers are fixed so two runs are comparable. Everything that
    would be a personal detail in a real document is already a
    placeholder — there is no reason for a cost benchmark to carry one.
    """
    return [
        Prompt(
            prompt_id="transcribe",
            system="Transcribe the document exactly. Output only its text.",
            user=(
                "PROVIDER: [PROVIDER_1]\n"
                "REFERENCE: [ACCOUNT_1]\n"
                "NOTICE DATE: 2026-02-26\n"
                "Your plan renews on 2027-03-07. The annual amount is [AMOUNT_1].\n"
                "To cancel before renewal, notify us by 2027-02-21."
            ),
            why="document reading, after local redaction",
        ),
        Prompt(
            prompt_id="deadline_extract",
            system=(
                "You extract obligations from household documents. Return JSON "
                "with provider, renewal_date, amount, and the exact evidence "
                "line for each field. Never invent a value."
            ),
            user=(
                "Document:\n"
                "PROVIDER: [PROVIDER_1]\n"
                "REFERENCE: [ACCOUNT_1]\n"
                "Your plan renews on 2027-03-07. The annual amount is [AMOUNT_1].\n"
                "To cancel before renewal, notify us by 2027-02-21."
            ),
            why="deadline extraction when the deterministic pass is unsure",
        ),
        Prompt(
            prompt_id="action_draft",
            system=(
                "You draft short, polite household correspondence. Use only the "
                "facts supplied. Never invent a number, a date, or a name."
            ),
            user=(
                "Draft a cancellation email to [PROVIDER_1] for [ACCOUNT_1], "
                "asking to cancel before the renewal on 2027-03-07. Mention the "
                "notice date of 2026-02-26."
            ),
            why="drafting the letter the user sends themselves",
        ),
        Prompt(
            prompt_id="memory_update",
            system=(
                "You turn a statement from the user into one structured fact. "
                'Return JSON with keys subject, kind, value, valid_from. If the '
                "statement is not an update, return an empty object."
            ),
            user=(
                'The user said: "I cancelled the gym membership in March." '
                "Today is 2026-10-20. The subject is gym and the kind is "
                "subscription_status."
            ),
            why="memory update when the statement is free text",
        ),
    ]


@dataclass(slots=True, frozen=True)
class TierSpec:
    """A resolved tier: role, model ID, and the catalog's published rates."""

    role: str
    model: str
    input_per_million: float
    output_per_million: float


@dataclass(slots=True)
class Tier:
    """The measured cost and latency of one tier."""

    role: str
    model: str
    input_per_million: float
    output_per_million: float
    calls: int = 0
    errors: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latencies: list[float] = field(default_factory=list)
    error_messages: list[str] = field(default_factory=list)
    by_prompt: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def mean_latency(self) -> float:
        """Mean wall-clock latency in seconds."""
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency(self) -> float:
        """Nearest-rank p95 in seconds. Empty when no call succeeded."""
        return p95(self.latencies)

    @property
    def cost_usd(self) -> float:
        """Total cost in USD from the measured token counts."""
        return (
            self.prompt_tokens / 1_000_000 * self.input_per_million
            + self.completion_tokens / 1_000_000 * self.output_per_million
        )

    @property
    def cost_per_call(self) -> float:
        """Mean cost of one successful call."""
        return self.cost_usd / self.calls if self.calls else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.role.capitalize(),
            "model": self.model,
            "input_per_million_usd": self.input_per_million,
            "output_per_million_usd": self.output_per_million,
            "calls": self.calls,
            "errors": self.errors,
            "retries": self.retries,
            "error_messages": self.error_messages,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "mean_latency_ms": round(self.mean_latency * 1000, 1),
            "p95_latency_ms": round(self.p95_latency * 1000, 1),
            "cost_usd": round(self.cost_usd, 8),
            "cost_per_call_usd": round(self.cost_per_call, 8),
            "cost_per_1k_calls_usd": round(self.cost_per_call * 1000, 4),
            "by_prompt": self.by_prompt,
        }


def p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile.

    With 12 samples that is the 12th value, not the 11th: the point of a
    tail number is that it is the tail, and this corpus is too small to
    interpolate honestly.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _pick_text_model(matches: list[dict], role: str) -> dict | None:
    """Choose deterministically from the catalog's matches for a role.

    The bench sends text, so a text-only model is preferred when the
    catalog offers one. Order within that is the catalog's, which is
    stable. The pinned role in `.env` is tried first, so this only runs
    when the pin is gone.
    """
    if not matches:
        return None
    text_only = [
        model
        for model in matches
        if "text->text" in str((model.get("architecture") or {}).get("modality", "")).lower()
    ]
    return (text_only or matches)[0]


def resolve_tiers(
    models: list[dict],
    *,
    pinned: dict[str, str] | None = None,
) -> list[TierSpec]:
    """Resolve the three tier slots against the live catalog.

    Args:
        models: The catalog records, as returned by `fetch_models`.
        pinned: The `LEDGER_MODEL_*` roles from `.env`, if any. A pinned
            role wins when the exact ID still exists in the catalog,
            because that is the model the agent actually calls.

    Returns:
        One `TierSpec` per role, in plan order.

    Raises:
        RuntimeError: if a role has no catalog match, or the match has no
            published price. The benchmark refuses to guess either.
    """
    pinned = pinned or {}
    resolved: list[TierSpec] = []

    for role in TIER_ROLES:
        pin = pinned.get(role, "").strip()
        chosen = find_model(models, pin) if pin else None
        if chosen is None:
            chosen = _pick_text_model(find_by_pattern(models, ROLE_PATTERNS[role]), role)
        if chosen is None:
            raise RuntimeError(
                f"no catalog model matches the {role!r} role "
                f"(patterns: {ROLE_PATTERNS[role]})"
            )

        prompt_price, completion_price = pricing_per_token(chosen)
        if prompt_price <= 0 or completion_price <= 0:
            raise RuntimeError(
                f"the catalog publishes no price for {model_id(chosen)!r} "
                f"({role} tier). Refusing to benchmark without a rate."
            )

        resolved.append(
            TierSpec(
                role=role,
                model=model_id(chosen),
                input_per_million=round(prompt_price * 1_000_000, 6),
                output_per_million=round(completion_price * 1_000_000, 6),
            )
        )

    return resolved


def _chat(
    model: str,
    prompt: Prompt,
    *,
    api_key: str,
    timeout: float,
    max_tokens: int,
) -> tuple[int, int, float]:
    """One chat completion. Returns (prompt tokens, completion tokens, seconds).

    Raises `RuntimeError` on an API or transport failure; `run_bench`
    records that as an error rather than dropping the call.
    """
    import httpx

    messages = []
    if prompt.system:
        messages.append({"role": "system", "content": prompt.system})
    messages.append({"role": "user", "content": prompt.user})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }

    started = time.perf_counter()
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{base_url()}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    elapsed = time.perf_counter() - started

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")

    data = response.json()
    usage = data.get("usage") or {}
    return (
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
        elapsed,
    )


def _is_transient(exc: Exception) -> bool:
    """Whether a failed call is worth trying again.

    A TLS EOF or a 5xx from the endpoint says nothing about the tier, so
    retrying it is honest. A 400 or a 401 does say something, and retrying
    it just doubles the wait before the same failure.
    """
    text = str(exc)
    return any(
        marker in text
        for marker in ("SSL", "EOF", "timed out", "timeout", "Connection", "HTTP 5")
    )


def run_bench(
    tiers: list[TierSpec],
    prompt_set: list[Prompt],
    *,
    repeats: int = 3,
    api_key: str = "",
    timeout: float = 120.0,
    max_tokens: int = MAX_COMPLETION_TOKENS,
    retries: int = 1,
) -> list[Tier]:
    """Run the benchmark.

    Args:
        tiers: Resolved tiers, from `resolve_tiers`.
        prompt_set: Representative prompts, from `prompts`.
        repeats: How many times to run each prompt against each tier, so
            the latency distribution means something.
        api_key: The Token Factory API key.
        timeout: Per-call timeout in seconds.
        max_tokens: Completion budget. Must leave room for a reasoning
            trace, or a reasoning model scores as fast and empty.
        retries: Extra attempts for a transient transport failure. A
            retried call still counts as one call; the retry count is
            reported so a flaky endpoint cannot hide in the table.

    Returns:
        One `Tier` per input tier, with per-call timings and token counts.
    """
    results: list[Tier] = []

    for spec in tiers:
        tier = Tier(
            role=spec.role,
            model=spec.model,
            input_per_million=spec.input_per_million,
            output_per_million=spec.output_per_million,
        )
        per_prompt: dict[str, dict[str, float]] = {}

        for prompt in prompt_set:
            latencies: list[float] = []
            prompt_tokens = 0
            completion_tokens = 0
            errors = 0

            for _ in range(repeats):
                attempt = 0
                while True:
                    try:
                        sent, received, elapsed = _chat(
                            spec.model,
                            prompt,
                            api_key=api_key,
                            timeout=timeout,
                            max_tokens=max_tokens,
                        )
                        break
                    except Exception as exc:  # one bad call, not a dead tier
                        if attempt < retries and _is_transient(exc):
                            attempt += 1
                            tier.retries += 1
                            time.sleep(0.5 * attempt)
                            continue
                        tier.errors += 1
                        errors += 1
                        tier.error_messages.append(f"{prompt.prompt_id}: {exc}")
                        sent = received = None
                        break

                if sent is None:
                    continue

                tier.calls += 1
                tier.prompt_tokens += sent
                tier.completion_tokens += received
                tier.latencies.append(elapsed)
                latencies.append(elapsed)
                prompt_tokens += sent
                completion_tokens += received

            per_prompt[prompt.prompt_id] = {
                "calls": len(latencies),
                "errors": errors,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "mean_latency_ms": round(statistics.mean(latencies) * 1000, 1)
                if latencies
                else 0.0,
                "p95_latency_ms": round(p95(latencies) * 1000, 1),
            }

        tier.by_prompt = per_prompt
        results.append(tier)

    return results


def render_table(results: list[Tier]) -> str:
    """A markdown table of the measured numbers."""
    lines = [
        "| Tier | Model | Calls | Errors | Retries | In tok | Out tok | Mean ms | p95 ms | "
        "Cost/call | $/1k calls |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for tier in results:
        lines.append(
            f"| {tier.role.capitalize()} | `{tier.model}` | {tier.calls} | {tier.errors} | "
            f"{tier.retries} | "
            f"{tier.prompt_tokens:,} | {tier.completion_tokens:,} | "
            f"{tier.mean_latency * 1000:.0f} | {tier.p95_latency * 1000:.0f} | "
            f"${tier.cost_per_call:.6f} | ${tier.cost_per_call * 1000:.2f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=3, help="per prompt per tier")
    parser.add_argument("--json", dest="json_out", default="eval/results/cost_latency.json")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve tiers and print the plan without spending anything",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="extra attempts for a transient transport failure",
    )
    args = parser.parse_args(argv)

    api_key = load_api_key()
    if not api_key:
        print("NEBIUS_API_KEY is not set (env or .env)", file=sys.stderr)
        return 1

    try:
        models = fetch_models(api_key)
        tiers = resolve_tiers(models, pinned=read_pinned_roles())
    except RuntimeError as exc:
        print(f"cannot resolve tiers: {exc}", file=sys.stderr)
        return 1

    prompt_set = prompts()
    print(f"catalog: {len(models)} models at {base_url()}")
    for spec in tiers:
        print(
            f"  {spec.role:5s} {spec.model:48s} "
            f"${spec.input_per_million:.2f}/1M in  ${spec.output_per_million:.2f}/1M out"
        )
    print(f"prompts: {', '.join(p.prompt_id for p in prompt_set)}")
    print(f"calls: {len(tiers) * len(prompt_set) * args.repeats}")

    if args.dry_run:
        print("dry run — nothing sent")
        return 0

    results = run_bench(
        tiers,
        prompt_set,
        repeats=args.repeats,
        retries=args.retries,
        api_key=api_key,
        timeout=args.timeout,
    )

    report = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "endpoint": base_url(),
        "catalog_models": len(models),
        "pricing_source": "live Token Factory catalog (pricing.prompt/pricing.completion)",
        "repeats": args.repeats,
        "retries": args.retries,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "prompts": [
            {"prompt_id": p.prompt_id, "why": p.why, "system": p.system, "user": p.user}
            for p in prompt_set
        ],
        "tiers": [tier.to_dict() for tier in results],
        "total_cost_usd": round(sum(tier.cost_usd for tier in results), 8),
    }

    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    print(render_table(results))
    print()
    print(f"total measured cost: ${report['total_cost_usd']:.6f}")
    print(f"written to {out}")

    if sum(tier.calls for tier in results) == 0:
        print("no call succeeded — refusing to report an empty table as a result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
