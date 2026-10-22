#!/usr/bin/env python3
"""Can a model extract fields from *redacted* text?

Phase 0 — Person B. The deciding experiment for the privacy architecture.

The recommended pipeline (Option C in the Phase 0 plan) is:

    document → local extraction → local redaction → model → structured fields

The document never leaves the sandbox in the clear: the local extraction
runs inside it, the deterministic redaction pass runs inside it, and the
model only ever receives text with placeholders where personal details
were. The placeholder→value map never leaves either.

That only works if the model can still do its job on the redacted text.
Two things could go wrong:

1. **The model loses the field.** The account number is now `[ACCOUNT_1]`,
   and a model that cannot reproduce that token has lost a value the
   pipeline needs.
2. **The model invents one.** A model asked for an account number and
   shown `[ACCOUNT_1]` might return a plausible-looking number instead
   of the placeholder. That is worse than losing it: the agent would
   draft a cancellation email with the wrong account number.

So this bench measures both, and it also reports **which fields the
model received in the clear** — the values the deterministic pass did not
catch. Those are the ones that leave the sandbox under Option C.

Usage:

    python eval/redacted_extract_bench.py --docs eval/synthetic_docs --count 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.scoring import SCORED_FIELDS, normalise_alnum
from eval.tokenfactory import load_api_key
from privacy_router.redact import local_redact

TOKEN_FACTORY_BASE_URL = os.environ.get(
    "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"
).rstrip("/")

# The completion budget.
#
# Super-VL is a *reasoning* model: it spends most of its completion
# tokens thinking, and those tokens count against `max_tokens`. At 1024,
# three documents in twenty came back with an empty `content` — the
# reasoning had used up the budget before the answer was written. The
# failure looked like a parsing bug, and it was not one.
#
# The budget has to cover the thinking and the answer.
MAX_COMPLETION_TOKENS = 4096

PROMPT = (
    "Read this redacted document and extract its fields.\n\n"
    "Reply with ONLY a JSON object, no prose and no code fence:\n"
    '{{"provider": ..., "renewal_date": ..., "amount": ..., '
    '"account_number": ..., "holder": ...}}\n\n'
    "Rules:\n"
    "- Personal details have been replaced by placeholders like [ACCOUNT_1] "
    "or [EMAIL_1]. Reproduce a placeholder EXACTLY, character for character. "
    "Do not guess, complete, or reconstruct the value it stands for.\n"
    "- If a field is not present in the document, use null.\n"
    "- Dates use YYYY-MM-DD. Amounts keep their currency symbol."
)

# Fields the model is asked for. `holder` is included on purpose: under
# Option C it is *not* redacted, so the model does receive it — which is
# the leak this bench exists to make visible.
REQUESTED_FIELDS = ["provider", "renewal_date", "amount", "account_number", "holder"]


@dataclass
class FieldOutcome:
    """How one field fared across the run."""

    name: str
    total: int = 0
    redacted: int = 0
    correct: int = 0
    hallucinated: int = 0
    missing: int = 0
    leaked_in_the_clear: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def hallucination_rate(self) -> float:
        return self.hallucinated / self.total if self.total else 0.0


@dataclass
class RunStats:
    docs: int = 0
    outcomes: dict[str, FieldOutcome] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def outcome(self, name: str) -> FieldOutcome:
        return self.outcomes.setdefault(name, FieldOutcome(name))


def find_placeholder(value: str, redactions) -> str | None:
    """The placeholder standing in for `value`, if there is one.

    The lookup is done on the **raw** text, not on a normalised form, and
    that matters more than it looks. Stripping punctuation from
    `devin.sullivan@example.com` gives `devinsullivaneexamplecom`, and the
    holder's name `Devin Sullivan` gives `devinsullivan` — so a
    normalised containment match decides that the *name* was redacted as
    the *email*, and the bench then reports the model as hallucinating a
    name it was never shown. Raw matching does not collide.
    """
    needle = value.strip().lower()
    if not needle:
        return None

    for redaction in redactions:
        if redaction.original.strip().lower() == needle:
            return redaction.placeholder

    # The redaction span may be wider than the value (an address line
    # around a street, a grouped number around its digits).
    for redaction in redactions:
        original = redaction.original.strip().lower()
        if original and needle in original:
            return redaction.placeholder

    return None


def values_match(field_name: str, returned: str, expected: str) -> bool:
    """Whether the model's answer matches the expected answer.

    Amounts are compared on their numbers: the ground truth says
    `$148.95 per month` and a model that answers `$148.95` has not made a
    mistake, it has normalised the phrase away. Comparing the strings
    would score a correct extraction as wrong.
    """
    if field_name == "amount":
        from eval.scoring import numeric_tokens

        expected_tokens = numeric_tokens(expected)
        returned_tokens = numeric_tokens(returned)
        return bool(expected_tokens & returned_tokens)

    return normalise_alnum(returned) == normalise_alnum(expected)


def call_model(model: str, text: str, api_key: str, timeout: float) -> tuple[dict, int, int]:
    """Ask the model to extract the fields. Returns (parsed, prompt_tokens, completion_tokens)."""
    import httpx

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"{PROMPT}\n\n---\n{text}"}],
        "max_tokens": MAX_COMPLETION_TOKENS,
        "temperature": 0.0,
    }

    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{TOKEN_FACTORY_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

    data = response.json()
    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    usage = data.get("usage", {})

    if not content.strip():
        # A reasoning model that ran out of budget says nothing at all.
        # Say *why*, because "the model returned nothing" is not a
        # diagnosis.
        reasoning = message.get("reasoning_content") or ""
        raise RuntimeError(
            f"the model returned no content (finish_reason="
            f"{data['choices'][0].get('finish_reason')!r}, "
            f"reasoning tokens={usage.get('completion_tokens_details', {}).get('reasoning_tokens')})"
            + (f"; it was still thinking: {reasoning[:80]!r}" if reasoning else "")
        )

    parsed = parse_json_object(content)
    if parsed is None:
        raise RuntimeError(f"could not parse a JSON object from: {content[:200]!r}")
    return parsed, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def parse_json_object(content: str) -> dict | None:
    """Pull the first JSON object out of a model response."""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"```$", "", content).strip()

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def run(docs_dir: Path, count: int, model: str, api_key: str, timeout: float) -> RunStats:
    """Run the bench over the corpus."""
    stats = RunStats()
    truth_files = sorted(docs_dir.glob("doc-*.json"))[:count]

    for truth_path in truth_files:
        text_path = docs_dir / f"{truth_path.stem}.txt"
        if not text_path.exists():
            continue

        text = text_path.read_text(encoding="utf-8")
        truth = json.loads(truth_path.read_text(encoding="utf-8"))

        # Step 1 and 2 of Option C, both inside the sandbox.
        redaction = local_redact(text)

        try:
            started = time.perf_counter()
            parsed, prompt_tokens, completion_tokens = call_model(
                model, redaction.text, api_key, timeout
            )
            stats.latencies.append(time.perf_counter() - started)
            stats.prompt_tokens += prompt_tokens
            stats.completion_tokens += completion_tokens
        except Exception as exc:
            stats.errors.append(f"{truth_path.stem}: {exc}")
            continue

        stats.docs += 1

        for field_name in REQUESTED_FIELDS:
            truth_value = truth.get(field_name)
            if not truth_value:
                continue

            outcome = stats.outcome(field_name)
            outcome.total += 1

            placeholder = find_placeholder(str(truth_value), redaction.redactions)
            returned = parsed.get(field_name)

            if placeholder is None:
                # The model received this value in the clear.
                outcome.leaked_in_the_clear += 1
                expected = str(truth_value)
            else:
                outcome.redacted += 1
                expected = placeholder

            if returned is None:
                outcome.missing += 1
                continue

            returned_text = str(returned).strip()
            if values_match(field_name, returned_text, expected):
                outcome.correct += 1
            else:
                if placeholder is not None:
                    outcome.hallucinated += 1
                if len(outcome.examples) < 4:
                    outcome.examples.append(f"expected {expected!r}, got {returned_text!r}")

    return stats


def render(stats: RunStats, model: str) -> str:
    import statistics

    mean_latency = statistics.mean(stats.latencies) if stats.latencies else 0.0
    lines = [
        f"**Model:** `{model}` · **documents:** {stats.docs} · "
        f"**mean latency:** {mean_latency:.2f}s · "
        f"**tokens:** {stats.prompt_tokens:,} in / {stats.completion_tokens:,} out",
        "",
        "| Field | Correct | Redacted | Model saw it in the clear | Hallucinated | Missing |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for field_name in REQUESTED_FIELDS:
        outcome = stats.outcomes.get(field_name)
        if outcome is None or outcome.total == 0:
            continue
        lines.append(
            f"| `{field_name}` | {outcome.correct}/{outcome.total} "
            f"({outcome.accuracy * 100:.0f}%) | {outcome.redacted} | "
            f"{outcome.leaked_in_the_clear} | {outcome.hallucinated} | {outcome.missing} |"
        )

    lines += ["", "### What the model got wrong", ""]
    any_examples = False
    for field_name in REQUESTED_FIELDS:
        outcome = stats.outcomes.get(field_name)
        if outcome is None or not outcome.examples:
            continue
        any_examples = True
        lines.append(f"- **`{field_name}`**")
        for example in outcome.examples:
            lines.append(f"  - {example}")
    if not any_examples:
        lines.append("_none_")

    if stats.errors:
        lines += ["", f"**{len(stats.errors)} error(s)**, first three:"]
        for error in stats.errors[:3]:
            lines.append(f"- {error}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", default="eval/synthetic_docs", help="corpus directory")
    parser.add_argument("--count", type=int, default=20, help="documents to sample")
    parser.add_argument("--model", default="nvidia/Nemotron-3.5-Super-VL", help="model to test")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout")
    parser.add_argument("--json", dest="json_out", default="", help="write raw results here")
    args = parser.parse_args(argv)

    api_key = load_api_key()
    if not api_key:
        print("NEBIUS_API_KEY is not set (env or .env)", file=sys.stderr)
        return 1

    docs_dir = Path(args.docs)
    if not docs_dir.exists():
        print(f"{docs_dir} does not exist — run: make synth", file=sys.stderr)
        return 1

    stats = run(docs_dir, args.count, args.model, api_key, args.timeout)
    print(render(stats, args.model))

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "model": args.model,
                    "docs": stats.docs,
                    "prompt_tokens": stats.prompt_tokens,
                    "completion_tokens": stats.completion_tokens,
                    "fields": {
                        name: {
                            "total": outcome.total,
                            "correct": outcome.correct,
                            "redacted": outcome.redacted,
                            "leaked_in_the_clear": outcome.leaked_in_the_clear,
                            "hallucinated": outcome.hallucinated,
                            "missing": outcome.missing,
                            "examples": outcome.examples,
                        }
                        for name, outcome in stats.outcomes.items()
                    },
                    "errors": stats.errors,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
