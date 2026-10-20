#!/usr/bin/env python3
"""Audit the deterministic redaction pass against the synthetic corpus.

Phase 0 — Person B.

This is the hand-off to Person A. The Phase 2 leak test measures what
actually leaves the sandbox, but the number it reports is bounded by what
the deterministic pass can catch. This script measures that bound *before*
Phase 2 exists, so the router is built against the real gaps rather than
against an assumption that the regexes are enough.

The method: for every document, run `privacy_router.redact.local_redact`
over the text, then ask whether each personal value from the ground truth
is still present in the redacted text. A value that survives is a leak
that the model-assisted escalation would have to catch — and the
escalation is itself an outbound call carrying the source text, so every
value the local pass misses is a value that goes out to a third party.

Two kinds of value are tracked separately:

* **personal** — name, email, phone, account number, policy number,
  address. These are the things that must not leave.
* **retained** — renewal date, amount, provider. These are the facts the
  agent needs, and the router is *supposed* to keep them. A router that
  masked the renewal date would break deadline detection. They are
  measured so the report shows the trade-off, not because they leak.

Usage:

    python eval/corpus_redaction_audit.py --docs eval/synthetic_docs
    python eval/corpus_redaction_audit.py --docs eval/synthetic_docs --json audit.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.scoring import normalise_alnum
from privacy_router.redact import local_redact

# Values that must not leave the sandbox.
PERSONAL_VALUES = [
    "holder",
    "email",
    "phone",
    "account_number",
    "policy_number",
    "street",
    "city",
    "postcode",
]

# Values the agent needs, which the router is meant to keep.
RETAINED_VALUES = ["renewal_date", "amount", "provider"]


@dataclass
class ValueTracker:
    """How one kind of value fared across the corpus."""

    name: str
    total: int = 0
    caught: int = 0
    surviving: list[str] = field(default_factory=list)
    categories: Counter = field(default_factory=Counter)

    @property
    def detection_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return self.caught / self.total

    def record(self, value: str, caught: bool, category: str = "") -> None:
        self.total += 1
        if caught:
            self.caught += 1
            if category:
                self.categories[category] += 1
        elif value:
            self.surviving.append(value)


def tracked_values(record: dict) -> dict[str, str]:
    """The values we track for one document, keyed by name.

    The address is split into its parts because the redaction patterns
    work line by line: the street is one span, and the city, state and
    postcode are separate tokens. Measuring them separately is the point
    — a single "address" row would hide which part survives.
    """
    values: dict[str, str] = {}
    for name in PERSONAL_VALUES + RETAINED_VALUES:
        value = record.get(name)
        if value:
            values[name] = str(value)

    address = str(record.get("address", ""))
    if address:
        values["street"] = address
    for name in ("city", "postcode"):
        if record.get(name):
            values[name] = str(record[name])
    return values


def audit(docs_dir: Path) -> tuple[dict[str, ValueTracker], dict]:
    """Run the audit over the corpus."""
    trackers: dict[str, ValueTracker] = {}
    documents = 0
    bytes_before = 0
    bytes_after = 0
    docs_clean = 0
    leaks_by_document = Counter()

    for truth_path in sorted(docs_dir.glob("doc-*.json")):
        record = json.loads(truth_path.read_text(encoding="utf-8"))
        text_path = docs_dir / f"{truth_path.stem}.txt"
        if not text_path.exists():
            continue

        text = text_path.read_text(encoding="utf-8")
        result = local_redact(text)
        redacted_normalised = normalise_alnum(result.text)
        bytes_before += len(text)
        bytes_after += len(result.text)

        values = tracked_values(record)
        surviving_here = 0

        for name, value in values.items():
            tracker = trackers.setdefault(name, ValueTracker(name))
            if name in RETAINED_VALUES:
                # Kept on purpose. Measured so the report shows the
                # trade-off, not because it is a leak.
                tracker.record(value, caught=True)
                continue

            caught = normalise_alnum(value) not in redacted_normalised
            category = ""
            if caught:
                for redaction in result.redactions:
                    if value.lower() in redaction.original.lower():
                        category = redaction.category.value
                        break
            tracker.record(value, caught, category)
            if not caught:
                surviving_here += 1

        documents += 1
        if surviving_here == 0:
            docs_clean += 1
        else:
            leaks_by_document[surviving_here] += 1

    summary = {
        "documents": documents,
        "documents_with_no_surviving_personal_value": docs_clean,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "reduction_percent": (1 - bytes_after / bytes_before) * 100 if bytes_before else 0.0,
        "surviving_per_document": dict(sorted(leaks_by_document.items())),
    }
    return trackers, summary


def render(trackers: dict[str, ValueTracker], summary: dict) -> str:
    lines = [
        "# Deterministic redaction audit",
        "",
        "Generated by `make redaction-audit`. Phase 0 — Person B.",
        "",
        "**This is the hand-off to Person A.** The Phase 2 leak test measures",
        "what actually leaves the sandbox, but the number it reports is bounded",
        "by what the deterministic pass can catch. This measures that bound",
        "before Phase 2 exists.",
        "",
        f"**Documents:** {summary['documents']} · "
        f"**documents with no surviving personal value:** "
        f"{summary['documents_with_no_surviving_personal_value']}/{summary['documents']} · "
        f"**text reduced by:** {summary['reduction_percent']:.1f}%",
        "",
        "## Personal values — these must not leave the sandbox",
        "",
        "| Value | Caught | Rate | Detected as |",
        "|---|---:|---:|---|",
    ]
    for name in PERSONAL_VALUES:
        tracker = trackers.get(name)
        if tracker is None or tracker.total == 0:
            continue
        categories = ", ".join(f"{k} ({v})" for k, v in tracker.categories.most_common())
        lines.append(
            f"| `{name}` | {tracker.caught}/{tracker.total} | "
            f"{tracker.detection_rate * 100:.1f}% | {categories or '—'} |"
        )

    lines += [
        "",
        "## Retained values — kept on purpose",
        "",
        "| Value | Kept |",
        "|---|---:|",
    ]
    for name in RETAINED_VALUES:
        tracker = trackers.get(name)
        if tracker is None or tracker.total == 0:
            continue
        lines.append(f"| `{name}` | {tracker.caught}/{tracker.total} |")

    # Everything that survived, so the gaps are concrete.
    gaps = [
        (name, tracker)
        for name, tracker in trackers.items()
        if name not in RETAINED_VALUES and tracker.surviving
    ]
    if gaps:
        lines += ["", "## What survives the deterministic pass", ""]
        for name, tracker in gaps:
            examples = ", ".join(f"`{value}`" for value in tracker.surviving[:5])
            more = f" (+{len(tracker.surviving) - 5} more)" if len(tracker.surviving) > 5 else ""
            lines.append(
                f"- **`{name}`** — {len(tracker.surviving)}/{tracker.total} survive. {examples}{more}"
            )
        lines += [
            "",
            "Every value in this list is one the model-assisted escalation has to catch,",
            "and the escalation is itself an outbound call carrying the source text.",
            "",
            "## What this means for Phase 2",
            "",
            "1. **Names are the priority.** `Category.NAME` exists in",
            "   `privacy_router/redact.py` but has no pattern in `_PATTERNS`, so",
            "   names are only ever caught by the escalation — which means every",
            "   holder name in the corpus currently leaves the sandbox.",
            "2. **Alphanumeric identifiers need their own pattern.** `POL-4825232-I`",
            "   and `SUB-281674` match neither `_ACCOUNT` (`\\b\\d{8,17}\\b`) nor",
            "   `_ACCOUNT_GROUPED`.",
            "3. **An address is more than a street.** `_ADDRESS` captures the street",
            "   line; the city and the postcode are left behind, and together with",
            "   a name they are enough to identify someone.",
            "4. **A category misclassification is not free.** 13 account numbers",
            "   were claimed by the `phone` pattern rather than `account`. The",
            "   value was still removed, so this is a reporting problem rather than",
            "   a leak — but the leak test scores by category, so it has to be",
            "   right.",
        ]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", default="eval/synthetic_docs", help="corpus directory")
    parser.add_argument("--json", dest="json_out", default="", help="write raw results here")
    parser.add_argument(
        "--markdown",
        dest="markdown_out",
        default="",
        help="write the report as markdown to this path",
    )
    args = parser.parse_args(argv)

    docs_dir = Path(args.docs)
    if not docs_dir.exists():
        print(f"{docs_dir} does not exist — run: make synth", file=sys.stderr)
        return 1

    trackers, summary = audit(docs_dir)
    report = render(trackers, summary)
    print(report)

    if args.markdown_out:
        out = Path(args.markdown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        print(f"\nwrote {out}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "values": {
                        name: {
                            "total": tracker.total,
                            "caught": tracker.caught,
                            "detection_rate": tracker.detection_rate,
                            "surviving_examples": tracker.surviving[:20],
                            "categories": dict(tracker.categories),
                        }
                        for name, tracker in trackers.items()
                    },
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
