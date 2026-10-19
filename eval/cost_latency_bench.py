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

Pricing is not hard-coded here. It is read from the Token Factory pricing
page at the time the benchmark runs, because a benchmark that silently
uses stale prices produces a confident wrong number. The rates go in
`eval/pricing.json`, and the script refuses to run without them.

Phase 3 implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TierResult", "main", "run_bench"]

PRICING_PATH = Path("eval/pricing.json")

# The three tiers from the plan, plus the OCR model.
DEFAULT_TIERS = [
    ("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "Nano"),
    ("nvidia/nemotron-3-super-120b-a12b", "Super"),
    ("nvidia/Nemotron-3-Ultra-550b-a55b", "Ultra"),
]


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

    @property
    def mean_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        return ordered[max(0, int(len(ordered) * 0.95) - 1)]

    def cost_usd(self, pricing: dict) -> float:
        """Cost in USD using the supplied per-million-token rates."""
        rates = pricing.get(self.tier) or pricing.get(self.model) or {}
        per_million_in = float(rates.get("input_per_million", 0.0))
        per_million_out = float(rates.get("output_per_million", 0.0))
        return (
            self.prompt_tokens / 1_000_000 * per_million_in
            + self.completion_tokens / 1_000_000 * per_million_out
        )


def run_bench(
    tiers: list[tuple[str, str]],
    prompts: list[dict],
    *,
    repeats: int = 3,
    api_key: str = "",
) -> list[TierResult]:
    """Run the benchmark.

    Args:
        tiers: (model_id, tier_name) pairs.
        prompts: Representative prompts.
        repeats: How many times to run each prompt against each tier, so
            the latency distribution means something.
        api_key: The Token Factory API key.

    Phase 3 implementation.
    """
    raise NotImplementedError("Phase 3 — see IMPLEMENTATION.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--json", dest="json_out", default="eval/results/cost_latency.json")
    args = parser.parse_args(argv)

    if not PRICING_PATH.exists():
        print(
            f"{PRICING_PATH} is missing.\n"
            "Fill in the current published per-million-token rates for each tier.\n"
            "The benchmark refuses to guess: a cost table with stale prices is a\n"
            "confident wrong number, which is worse than no number.",
            file=os.sys.stderr,
        )
        return 1

    print("Phase 3 — not implemented yet. See IMPLEMENTATION.md.")
    print(f"results would be written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
