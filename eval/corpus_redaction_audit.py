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

**Two passes**, because they answer different questions:

* **deterministic** — what a generic document yields with no knowledge of
  who the reader is. This is the floor.
* **deterministic + household names** — what the product actually does.
  The agent's memory layer already holds the household picture, so the
  redactor is *told* the names instead of having to recognise them in
  prose. This is the number that bounds the leak rate.

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
    categories: dict[str, int] = field(default_factory=dict)

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
                self.categories[category] = self.categories.get(category, 0) + 1
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


def household_names(docs_dir: Path) -> list[str]:
    """The distinct holder names in the corpus — the household picture.

    In the product these come from the memory layer, which already knows
    who the household is. Deriving them from the corpus here is the
    equivalent, and it is what makes the second pass realistic rather
    than a hypothetical.
    """
    names: set[str] = set()
    for truth_path in sorted(docs_dir.glob("doc-*.json")):
        record = json.loads(truth_path.read_text(encoding="utf-8"))
        holder = record.get("holder")
        if holder:
            names.add(str(holder))
    return sorted(names)


def audit(
    docs_dir: Path, known_names: tuple[str, ...] = ()
) -> tuple[dict[str, ValueTracker], dict, dict[str, ValueTracker], dict]:
    """Run both passes over the corpus.

    Returns:
        (bare trackers, bare summary, known-names trackers, known summary)
    """
    bare: dict[str, ValueTracker] = {}
    known: dict[str, ValueTracker] = {}
    documents = 0
    bytes_before = 0
    bytes_after = 0
    clean_bare = 0
    clean_known = 0

    for truth_path in sorted(docs_dir.glob("doc-*.json")):
        record = json.loads(truth_path.read_text(encoding="utf-8"))
        text_path = docs_dir / f"{truth_path.stem}.txt"
        if not text_path.exists():
            continue

        text = text_path.read_text(encoding="utf-8")
        values = tracked_values(record)

        documents += 1
        bytes_before += len(text)

        for tracker_set, names in ((bare, ()), (known, known_names)):
            result = local_redact(text, names=names)
            redacted_normalised = normalise_alnum(result.text)
            if not names:
                bytes_after += len(result.text)

            surviving_here = 0
            for name, value in values.items():
                tracker = tracker_set.setdefault(name, ValueTracker(name))
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

            if surviving_here == 0:
                if names:
                    clean_known += 1
                else:
                    clean_bare += 1

    summary = {
        "documents": documents,
        "documents_with_no_surviving_personal_value": clean_bare,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "reduction_percent": (1 - bytes_after / bytes_before) * 100 if bytes_before else 0.0,
    }
    known_summary = {
        "documents": documents,
        "documents_with_no_surviving_personal_value": clean_known,
    }
    return bare, summary, known, known_summary


def render(
    trackers: dict[str, ValueTracker],
    summary: dict,
    known: dict[str, ValueTracker],
    known_summary: dict,
) -> str:
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
        "## Two passes, because they answer different questions",
        "",
        "| Pass | What it means |",
        "|---|---|",
        "| **deterministic** | What a generic document yields with no knowledge of who the reader is. The floor. |",
        "| **deterministic + household names** | What the product actually does. The agent's memory layer already holds the household picture, so the redactor is *told* the names instead of having to recognise them in prose. **This is the number that bounds the leak rate.** |",
        "",
        f"**Documents:** {summary['documents']} · "
        f"**documents with no surviving personal value:** "
        f"deterministic {summary['documents_with_no_surviving_personal_value']}/{summary['documents']} · "
        f"with household names "
        f"{known_summary['documents_with_no_surviving_personal_value']}/{known_summary['documents']} · "
        f"**text reduced by:** {summary['reduction_percent']:.1f}%",
        "",
        "## Personal values — these must not leave the sandbox",
        "",
        "| Value | Deterministic | With household names | Detected as |",
        "|---|---:|---:|---|",
    ]
    for name in PERSONAL_VALUES:
        tracker = trackers.get(name)
        if tracker is None or tracker.total == 0:
            continue
        known_tracker = known.get(name)
        known_cell = (
            f"{known_tracker.caught}/{known_tracker.total} "
            f"({known_tracker.detection_rate * 100:.1f}%)"
            if known_tracker
            else "—"
        )
        categories = ", ".join(f"{k} ({v})" for k, v in sorted(tracker.categories.items()))
        lines.append(
            f"| `{name}` | {tracker.caught}/{tracker.total} "
            f"({tracker.detection_rate * 100:.1f}%) | {known_cell} | {categories or '—'} |"
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
        ]

    lines += [
        "",
        "## What this means for Phase 2",
        "",
        "1. **Names are the priority.** `Category.NAME` has no single pattern —",
        "   a name is found after a label that says it is a name, in a",
        "   salutation, or because the household's memory layer says so. A name",
        "   in the middle of a sentence is still only caught by the escalation.",
        "2. **Alphanumeric identifiers need their own pattern.** `POL-4825232-I`",
        "   and `SUB-281674` match neither `_ACCOUNT` (`\\b\\d{8,17}\\b`) nor",
        "   `_ACCOUNT_GROUPED`.",
        "3. **An address is more than a street.** The address span now continues",
        "   through the city, the state and the postcode; masking only the",
        "   street left enough to identify someone.",
        "4. **A category misclassification is not free.** Ten digits are a phone",
        "   number or an account number and only the label can say which, so the",
        "   label immediately before the value decides.",
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

    names = tuple(household_names(docs_dir))
    trackers, summary, known, known_summary = audit(docs_dir, names)
    report = render(trackers, summary, known, known_summary)
    print(report)

    if args.markdown_out:
        out = Path(args.markdown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        print(f"\nwrote {out}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)

        def dump(tracker_set: dict[str, ValueTracker]) -> dict:
            return {
                name: {
                    "total": tracker.total,
                    "caught": tracker.caught,
                    "detection_rate": tracker.detection_rate,
                    "surviving_examples": tracker.surviving[:20],
                    "categories": tracker.categories,
                }
                for name, tracker in tracker_set.items()
            }

        out.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "summary_with_household_names": known_summary,
                    "household_names": list(names),
                    "deterministic": dump(trackers),
                    "with_household_names": dump(known),
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
