#!/usr/bin/env python3
"""Run the Phase 1 exit condition and show what it produced.

Phase 1 — Person B.

    document ──► ingestion ──► memory ──► deadlines ──► action

Run it:

    make demo                       # over the synthetic corpus
    python agent/demo.py <path>     # over one document of your own

It prints what the pipeline found, what it will tell the user, and the
draft it wrote — and it prints the *redacted* version of the document,
because that is the version that leaves the sandbox. The real values are
in the draft, because the draft is for the user, in the user's own
channel, which is inside the boundary.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.memory.hermes_memory import HouseholdMemory
from agent.pipeline import process_document


def show(path: Path, memory: HouseholdMemory, today: date) -> bool:
    """Run the pipeline over one document and print the result."""
    result = process_document(path, memory=memory, today=today)

    print("=" * 78)
    print(f"{path}")
    print("=" * 78)

    if result.warnings:
        print(f"  warnings    : {', '.join(result.warnings)}")

    if not result.ok:
        print("  → nothing to do with this document")
        print()
        return False

    item = result.item
    assert item is not None

    print(f"  provider    : {item.provider}")
    print(f"  kind        : {item.kind}")
    print(f"  holder      : {item.holder}")
    print(f"  reference   : {item.reference}")
    if item.amount is not None:
        print(f"  amount      : {item.amount:.2f}")
    print()

    print("  deadlines:")
    for deadline in result.deadlines:
        when = "PAST" if deadline.is_past else f"in {deadline.days_until(today)} days"
        print(f"    - {deadline.kind.value:<22} {deadline.date}  ({when}, "
              f"confidence {deadline.confidence:.2f})")
        print(f"      evidence: {deadline.evidence[:100]}")
    print()

    action = result.action
    assert action is not None
    print(f"  action      : {action.kind.value} — {action.title}")
    print()
    print("  --- the draft, as the user would see it ---")
    for line in action.body.splitlines():
        print(f"  {line}")
    print()

    print("  --- the document as the model sees it (redacted) ---")
    for line in result.document.text.splitlines()[:12]:
        print(f"  {line}")
    print()

    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="documents to process")
    parser.add_argument("--docs", default="eval/synthetic_docs", help="the synthetic corpus")
    parser.add_argument("--count", type=int, default=3, help="how many corpus documents")
    parser.add_argument("--today", default="2026-10-20", help="the reference date")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.today)

    paths = [Path(p) for p in args.paths]
    if not paths:
        corpus = Path(args.docs)
        if not corpus.exists():
            print(f"{corpus} does not exist — run: make synth", file=sys.stderr)
            return 1
        paths = sorted(corpus.glob("doc-*.txt"))[: args.count]

    if not paths:
        print("nothing to process", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        memory = HouseholdMemory(Path(tmp) / "memory.db")
        try:
            ok = 0
            for path in paths:
                if show(path, memory, today):
                    ok += 1

            print("=" * 78)
            print(f"{ok}/{len(paths)} documents produced a deadline and a draft")
            print(f"the household is tracking {len(memory.all_items())} obligations")
            print("=" * 78)
        finally:
            memory.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
