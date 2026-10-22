#!/usr/bin/env python3
"""The leak test — router ON vs OFF.

Phase 1 (built now) / Phase 2 (the measurement) — Person B, with Person A.

The project's headline claim is *"personal details never reach a Super/Ultra
tier or a third-party service"*. This is the script that measures it.

WHAT IS MEASURED
-----------------
The Phase 1 pipeline is entirely local: there is no HTTP client anywhere in
`agent/`, so the agent's current egress is *zero*. There is therefore no
outbound request to capture yet. What the architecture puts on the wire is
the **model-facing text** — the document as the model would receive it — and
that is what this measures, under two conditions:

  router OFF   the extracted text with no redaction: what the model would
               receive if the redaction step were absent
  router ON    the redacted text: what the model actually receives under
               Option C (extract locally, redact locally, then read)

Both payloads come from the SAME real pipeline run over the SAME documents,
differing only in one argument (`redact=False` / `redact=True`), so the
comparison isolates the redaction step and nothing else. The script also
checks that the two are exactly reversible — that the redaction is a pure
text transform — and reports it.

A payload counts as leaking when a personal value from the corpus ground
truth is still present in it. The measurement is against the ground truth
rather than against a pattern, so it cannot be fooled by a redaction that
removes the wrong thing.

The router must also not destroy what the model needs, so the retained
values — the renewal date, the amount, the provider — are measured too. A
router that removes everything is not a router.

In Phase 2, when the router is a supervisor middleware attached to real
traffic, the same scorer runs against captured request bodies, and the ON
condition becomes the middleware's output rather than the in-process pass.

Usage:

    python eval/leak_test.py --docs eval/synthetic_docs --count 200
    python eval/leak_test.py --docs eval/synthetic_docs --count 200 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.ingestion.parse_documents import parse_document
from eval.corpus_redaction_audit import (
    PERSONAL_VALUES,
    RETAINED_VALUES,
    household_names,
    tracked_values,
)
from eval.scoring import normalise_alnum

ROUTER_OFF = "router_off"
ROUTER_ON = "router_on"

CONDITION_LABELS = {
    ROUTER_OFF: "router OFF — no redaction",
    ROUTER_ON: "router ON — redaction applied",
}


@dataclass
class ConditionStats:
    """One condition's numbers."""

    name: str
    documents: int = 0
    payloads_with_leak: int = 0
    bytes_total: int = 0
    leaked: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)
    kept_totals: dict[str, int] = field(default_factory=dict)

    @property
    def leak_rate(self) -> float:
        """The fraction of outbound payloads carrying a personal detail."""
        if not self.documents:
            return 0.0
        return self.payloads_with_leak / self.documents

    @property
    def bytes_per_document(self) -> float:
        if not self.documents:
            return 0.0
        return self.bytes_total / self.documents

    def to_dict(self) -> dict:
        return {
            "condition": self.name,
            "documents": self.documents,
            "payloads_with_personal_detail": self.payloads_with_leak,
            "leak_rate": round(self.leak_rate, 4),
            "bytes_per_document": round(self.bytes_per_document, 1),
            "leaked_by_value": dict(sorted(self.leaked.items())),
            "present_by_value": dict(sorted(self.totals.items())),
            "retained_kept": dict(sorted(self.kept.items())),
            "retained_total": dict(sorted(self.kept_totals.items())),
        }


@dataclass
class RunResult:
    """The whole run."""

    stats: dict[str, ConditionStats] = field(default_factory=dict)
    reversible: int = 0
    """Documents where the redaction was exactly reversible."""

    checked: int = 0
    """Documents where reversibility was checked."""

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "conditions": {k: v.to_dict() for k, v in self.stats.items()},
            "reversible": self.reversible,
            "reversibility_checked": self.checked,
            "notes": list(self.notes),
        }


def _score(payload: str, values: dict[str, str], stats: ConditionStats) -> bool:
    """Record which tracked values are present in a payload.

    Returns:
        True when at least one personal value survived — a leak.
    """
    haystack = normalise_alnum(payload)
    leaked = False

    for name, value in values.items():
        if name in RETAINED_VALUES:
            continue
        stats.totals[name] = stats.totals.get(name, 0) + 1
        if value and normalise_alnum(value) in haystack:
            stats.leaked[name] = stats.leaked.get(name, 0) + 1
            leaked = True

    for name, value in values.items():
        if name not in RETAINED_VALUES:
            continue
        stats.kept_totals[name] = stats.kept_totals.get(name, 0) + 1
        if value and normalise_alnum(value) in haystack:
            stats.kept[name] = stats.kept.get(name, 0) + 1

    return leaked


def _document_paths(docs_dir: Path, truth_path: Path, use_ocr: bool) -> Path | None:
    """The file to feed the pipeline for one corpus document.

    The rendered scan is the document a user actually has, so it is the
    default; the source text is the fallback when a corpus has no renders.
    """
    if use_ocr:
        rendered = docs_dir / f"{truth_path.stem}.png"
        if rendered.exists():
            return rendered
    source = docs_dir / f"{truth_path.stem}.txt"
    return source if source.exists() else None


def run_leak_test(
    docs_dir: Path,
    count: int = 0,
    *,
    use_ocr: bool = True,
) -> RunResult:
    """Run the leak test over the corpus.

    Args:
        docs_dir: The corpus directory.
        count: How many documents. 0 means all of them.
        use_ocr: Read the rendered scan rather than the source text.

    Returns:
        A `RunResult`.
    """
    truth_paths = sorted(docs_dir.glob("doc-*.json"))
    if not truth_paths:
        raise RuntimeError(f"no corpus documents found in {docs_dir} — run: make synth")
    if count:
        truth_paths = truth_paths[:count]

    # The household picture. In the product these come from the memory
    # layer, which already knows who the household is; deriving them from
    # the corpus here is the equivalent, and it is what makes the ON
    # condition realistic rather than a hypothetical.
    names = household_names(docs_dir)

    result = RunResult(
        stats={ROUTER_OFF: ConditionStats(ROUTER_OFF), ROUTER_ON: ConditionStats(ROUTER_ON)}
    )

    for truth_path in truth_paths:
        record = json.loads(truth_path.read_text(encoding="utf-8"))
        values = tracked_values(record)

        doc_path = _document_paths(docs_dir, truth_path, use_ocr)
        if doc_path is None:
            result.notes.append(f"{truth_path.stem}: no document file")
            continue

        off_doc = parse_document(doc_path, redact=False)
        on_doc = parse_document(doc_path, household_names=names, redact=True)

        if not off_doc.text.strip():
            result.notes.append(f"{truth_path.stem}: no text extracted")
            continue

        for condition, payload in (
            (ROUTER_OFF, off_doc.text),
            (ROUTER_ON, on_doc.text),
        ):
            stats = result.stats[condition]
            stats.documents += 1
            stats.bytes_total += len(payload.encode("utf-8"))
            if _score(payload, values, stats):
                stats.payloads_with_leak += 1

        # The redaction must be a pure text transform: the OFF payload and
        # the ON payload with its placeholders resolved have to be the same
        # bytes. If they ever differ, the two conditions are not
        # comparable and the measurement is not the one it claims to be.
        result.checked += 1
        if on_doc.redactions.resolve(on_doc.text) == off_doc.text:
            result.reversible += 1

    return result


def render_summary(result: RunResult) -> str:
    """The headline table."""
    off = result.stats[ROUTER_OFF]
    on = result.stats[ROUTER_ON]

    lines = [
        "| Condition | Documents | Payloads with personal detail | Leak rate | Bytes/doc |",
        "|---|---:|---:|---:|---:|",
    ]
    for condition in (ROUTER_OFF, ROUTER_ON):
        stats = result.stats[condition]
        lines.append(
            f"| {CONDITION_LABELS[condition]} | {stats.documents} | "
            f"{stats.payloads_with_leak} | {stats.leak_rate * 100:.1f}% | "
            f"{stats.bytes_per_document:.0f} |"
        )

    reduction = off.leak_rate - on.leak_rate
    lines.append("")
    lines.append(
        f"**Leak rate falls from {off.leak_rate * 100:.1f}% to {on.leak_rate * 100:.1f}%** "
        f"— a reduction of {reduction * 100:.1f} percentage points."
    )
    return "\n".join(lines)


def render_by_value(result: RunResult) -> str:
    """Per-value detail: what leaked, and what the router kept."""
    off = result.stats[ROUTER_OFF]
    on = result.stats[ROUTER_ON]

    lines = [
        "### Personal values — these must not leave the sandbox",
        "",
        "| Value | Present in corpus | Leaked with router OFF | Leaked with router ON |",
        "|---|---:|---:|---:|",
    ]
    for name in PERSONAL_VALUES:
        total = off.totals.get(name, 0)
        if not total:
            continue
        lines.append(
            f"| `{name}` | {total} | {off.leaked.get(name, 0)} | {on.leaked.get(name, 0)} |"
        )

    lines += [
        "",
        "### Retained values — the router must not destroy these",
        "",
        "| Value | Present in corpus | Kept with router OFF | Kept with router ON |",
        "|---|---:|---:|---:|",
    ]
    for name in RETAINED_VALUES:
        total = off.kept_totals.get(name, 0)
        if not total:
            continue
        lines.append(
            f"| `{name}` | {total} | {off.kept.get(name, 0)} | {on.kept.get(name, 0)} |"
        )

    return "\n".join(lines)


def render_checks(result: RunResult) -> str:
    """The self-checks that make the number trustworthy."""
    lines = []
    if result.checked:
        lines.append(
            f"- **Redaction is exactly reversible: {result.reversible}/{result.checked}** "
            "documents. The ON payload with its placeholders resolved is byte-identical to "
            "the OFF payload, so the two conditions differ by the redaction step and "
            "nothing else."
        )
        if result.reversible != result.checked:
            lines.append(
                f"  - **{result.checked - result.reversible} document(s) diverged** — the "
                "measurement below is not comparable for those."
            )
    if result.notes:
        lines.append(f"- {len(result.notes)} document(s) were skipped:")
        for note in result.notes[:5]:
            lines.append(f"  - `{note}`")
    return "\n".join(lines) if lines else "- no self-checks ran"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", default="eval/synthetic_docs", help="the corpus directory")
    parser.add_argument("--count", type=int, default=0, help="documents to test (0 = all)")
    parser.add_argument(
        "--text",
        action="store_true",
        help="use the source text instead of the rendered scan",
    )
    parser.add_argument("--json", dest="json_out", default="", help="write raw results here")
    args = parser.parse_args(argv)

    docs_dir = Path(args.docs)
    if not docs_dir.exists():
        print(f"{docs_dir} does not exist — run: make synth", file=sys.stderr)
        return 1

    print(f"Leak test — {docs_dir}\n")
    result = run_leak_test(docs_dir, args.count, use_ocr=not args.text)

    print(render_summary(result))
    print()
    print(render_by_value(result))
    print()
    print("### Self-checks")
    print(render_checks(result))

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
