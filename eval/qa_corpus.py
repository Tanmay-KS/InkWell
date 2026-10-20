#!/usr/bin/env python3
"""Validate the synthetic corpus.

Phase 0 — Person B.

The corpus is the raw material for the Phase 2 leak test and the Phase 3
evaluations. If it is malformed, every number downstream is wrong in a way
that is hard to see, so this script asserts the properties the rest of the
project assumes:

* the corpus is the expected size, and every document kind is represented
* the *format* variety the README promises is actually present — account
  numbers written four different ways, phone numbers four ways, email
  addresses three ways
* renewal dates cover the range the pipeline has to handle, including
  documents that arrived late
* no two documents share a holder and an account number
* **the ground truth matches the document** — every value in the `.json`
  really is in the `.txt`. The OCR benchmark scores against this ground
  truth, so a stale or wrong field would silently corrupt every number it
  produces
* the rendered images match the documents

Usage:

    python eval/qa_corpus.py --docs eval/synthetic_docs --count 200
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.scoring import normalise_alnum

# The fields the ground truth must agree with the document on.
GROUND_TRUTH_FIELDS = [
    "holder",
    "email",
    "phone",
    "account_number",
    "provider",
    "renewal_date",
    "amount",
]

# The document kinds the generator produces.
EXPECTED_KINDS = ["insurance", "membership", "subscription", "licence", "medical", "utility"]

# Renewal-date buckets, in days from the reference date. The corpus has to
# reach past the "next month" case, or the deadline logic is never tested
# against a distant renewal.
DATE_BUCKETS = {
    "already past (-40..0)": (-40, 0),
    "soon (1..30)": (1, 30),
    "this quarter (31..120)": (31, 120),
    "later (121..400)": (121, 400),
}


@dataclass
class Check:
    """One assertion and its outcome."""

    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", skipped: bool = False) -> None:
        self.checks.append(Check(name, ok, detail, skipped))

    @property
    def ok(self) -> bool:
        """Skipped checks do not fail the report."""
        return all(check.ok for check in self.checks if not check.skipped)

    def render(self) -> str:
        lines = []
        for check in self.checks:
            mark = "SKIP" if check.skipped else "PASS" if check.ok else "FAIL"
            detail = f" — {check.detail}" if check.detail else ""
            lines.append(f"[{mark}] {check.name}{detail}")
        return "\n".join(lines)


# The coverage assertions below only mean something on a corpus large
# enough to expect every kind, every format and every date bucket. A
# three-document smoke test is a smoke test, not a coverage report.
FULL_COVERAGE_MIN = 100


def account_format(value: str) -> str:
    """Classify an account number's written format."""
    if re.fullmatch(r"\d+", value):
        return "plain digits"
    if re.fullmatch(r"\d{4}-\d{4}", value):
        return "dashed"
    if re.fullmatch(r"\d{4} \d{4}", value):
        return "grouped"
    if re.search(r"[A-Za-z]", value):
        return "alphanumeric"
    return "other"


def phone_format(value: str) -> str:
    """Classify a phone number's written format."""
    if value.startswith("+1"):
        return "e164 (+1)"
    if value.startswith("("):
        return "parenthesised"
    if re.fullmatch(r"\d{3}-\d{3}-\d{4}", value):
        return "dashed"
    if re.fullmatch(r"\d{10}", value):
        return "plain digits"
    return "other"


def email_format(value: str) -> str:
    """Classify an email address's written format."""
    local = value.split("@", 1)[0]
    if "+" in local:
        return "plus-addressed"
    if "." in local:
        return "dotted"
    return "initial"


def load_corpus(docs_dir: Path) -> list[dict]:
    """Load every ground-truth record in the corpus."""
    records = []
    for path in sorted(docs_dir.glob("doc-*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def check_formats(
    records: list[dict], report: Report, full_coverage: bool = True
) -> dict[str, dict[str, int]]:
    """Assert the format variety the README promises is actually present."""
    counters = {
        "account_number": {},
        "phone": {},
        "email": {},
    }
    for record in records:
        for field_name, classifier in (
            ("account_number", account_format),
            ("phone", phone_format),
            ("email", email_format),
        ):
            label = classifier(str(record.get(field_name, "")))
            counters[field_name][label] = counters[field_name].get(label, 0) + 1

    expected = {
        "account_number": {"plain digits", "dashed", "grouped", "alphanumeric"},
        "phone": {"plain digits", "dashed", "parenthesised", "e164 (+1)"},
        "email": {"dotted", "initial", "plus-addressed"},
    }

    for field_name, wanted in expected.items():
        present = set(counters[field_name])
        missing = wanted - present
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counters[field_name].items()))
        if missing and not full_coverage:
            report.add(
                f"{field_name}: every format represented",
                True,
                f"not asserted on a corpus of {len(records)} — missing {sorted(missing)}",
                skipped=True,
            )
            continue
        report.add(
            f"{field_name}: every format represented",
            not missing,
            f"missing {sorted(missing)}" if missing else detail,
        )

    return counters


def check_dates(records: list[dict], report: Report, full_coverage: bool = True) -> None:
    """Assert the renewal dates cover the range the pipeline must handle."""
    buckets = {name: 0 for name in DATE_BUCKETS}
    for record in records:
        days = int(record.get("days_until_renewal", 0))
        for name, (low, high) in DATE_BUCKETS.items():
            if low <= days <= high:
                buckets[name] += 1
                break

    empty = [name for name, count in buckets.items() if count == 0]
    detail = ", ".join(f"{k}={v}" for k, v in buckets.items())
    if empty and not full_coverage:
        report.add(
            "renewal dates cover every bucket",
            True,
            f"not asserted on a corpus of {len(records)} — empty {empty}",
            skipped=True,
        )
        return
    report.add(
        "renewal dates cover every bucket",
        not empty,
        f"empty {empty}" if empty else detail,
    )


def check_duplicates(records: list[dict], report: Report) -> None:
    """Assert no two documents share a holder and an account number."""
    seen: dict[tuple[str, str], str] = {}
    duplicates = []
    for record in records:
        key = (str(record.get("holder", "")), str(record.get("account_number", "")))
        if key in seen:
            duplicates.append(f"{seen[key]} and {record.get('doc_id')} share {key[0]}/{key[1]}")
        seen[key] = str(record.get("doc_id", ""))
    report.add(
        "no duplicate holder/account pairs",
        not duplicates,
        "; ".join(duplicates[:3]) if duplicates else f"{len(seen)} unique pairs",
    )


def check_ground_truth(docs_dir: Path, records: list[dict], report: Report) -> None:
    """Assert the ground truth matches the document text.

    The OCR benchmark scores extracted text against these values, so a
    ground truth that disagrees with the document is a corrupted
    benchmark, not a small inconsistency.
    """
    mismatches = []
    for record in records:
        doc_id = str(record.get("doc_id", ""))
        text_path = docs_dir / f"{doc_id}.txt"
        if not text_path.exists():
            mismatches.append(f"{doc_id}: no text file")
            continue
        text = normalise_alnum(text_path.read_text(encoding="utf-8"))
        for field_name in GROUND_TRUTH_FIELDS:
            value = record.get(field_name)
            if not value:
                continue
            if field_name == "amount":
                from eval.scoring import numeric_tokens

                tokens = numeric_tokens(text_path.read_text(encoding="utf-8"))
                digits = re.sub(r"[^0-9.]", "", str(value))
                if digits not in tokens and digits.rstrip("0").rstrip(".") not in tokens:
                    mismatches.append(f"{doc_id}.{field_name}: {value!r} not in document")
                continue
            if normalise_alnum(str(value)) not in text:
                mismatches.append(f"{doc_id}.{field_name}: {value!r} not in document")

    report.add(
        "ground truth agrees with the document text",
        not mismatches,
        "; ".join(mismatches[:3]) if mismatches else f"{len(records)} documents checked",
    )


def check_images(docs_dir: Path, records: list[dict], report: Report) -> None:
    """Assert every document has a rendered image, and it is really an image."""
    missing = []
    not_images = []
    for record in records:
        doc_id = str(record.get("doc_id", ""))
        image_path = docs_dir / f"{doc_id}.png"
        if not image_path.exists():
            missing.append(doc_id)
            continue
        header = image_path.read_bytes()[:8]
        if not header.startswith(b"\x89PNG\r\n\x1a\n"):
            not_images.append(doc_id)

    report.add(
        "every document has a rendered PNG",
        not missing,
        f"missing {len(missing)}: {missing[:3]}" if missing else f"{len(records)} images",
    )
    report.add(
        "the rendered files are really PNGs",
        not not_images,
        f"not PNG: {not_images[:3]}" if not_images else "all PNG",
    )


def check_reproducible(docs_dir: Path, report: Report, seed: int, today: str, count: int) -> None:
    """Assert the same seed and the same reference date give the same corpus.

    This is the reproducibility contract. The generator derives every
    renewal date from the reference date, so the date has to be pinned —
    otherwise "the same seed" does not mean the same corpus.
    """
    generator = Path(__file__).resolve().parent / "gen_synthetic_docs.py"
    sample = min(count, 5)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "corpus"
        proc = subprocess.run(
            [
                sys.executable,
                str(generator),
                "--out",
                str(out),
                "--count",
                str(sample),
                "--seed",
                str(seed),
                "--today",
                today,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            report.add("corpus is reproducible", False, proc.stderr.strip()[:200])
            return

        differences = []
        compared = 0
        for path in sorted(out.glob("doc-*.txt")):
            original = docs_dir / path.name
            if not original.exists():
                # The corpus under test is smaller than the sample; there
                # is nothing to compare against.
                continue
            compared += 1
            if original.read_bytes() != path.read_bytes():
                differences.append(f"{path.name} differs")

        if compared == 0:
            report.add("corpus is reproducible", False, "no documents to compare")
            return

        report.add(
            "corpus is reproducible (same seed + same reference date)",
            not differences,
            "; ".join(differences[:3]) if differences else f"byte-identical ({compared} docs)",
        )


def check_no_clipping(docs_dir: Path, records: list[dict], report: Report) -> None:
    """Assert the rendered page contains the whole document.

    The renderer used to truncate long lines at the right edge of the
    page and drop lines that ran past the bottom. The rendered "scan" was
    therefore a *partial* document, and every OCR candidate was judged on
    a page missing text — the benchmark was measuring the renderer's bug.

    Two things are checked here: that wrapping the source text is
    lossless, and that the image is tall enough to hold every wrapped
    line.
    """
    from PIL import Image

    from eval.gen_synthetic_docs import LINE_HEIGHT, MARGIN, wrap_lines

    lossy = []
    too_short = []

    for record in records:
        doc_id = str(record.get("doc_id", ""))
        text_path = docs_dir / f"{doc_id}.txt"
        image_path = docs_dir / f"{doc_id}.png"
        if not text_path.exists() or not image_path.exists():
            continue

        text = text_path.read_text(encoding="utf-8")
        wrapped = wrap_lines(text)

        rebuilt = re.sub(r"\s+", " ", " ".join(wrapped)).strip()
        original = re.sub(r"\s+", " ", text).strip()
        if rebuilt != original:
            lossy.append(doc_id)

        needed = MARGIN * 2 + len(wrapped) * LINE_HEIGHT
        with Image.open(image_path) as image:
            height = image.size[1]
        if height < needed:
            too_short.append(f"{doc_id} ({height} < {needed})")

    report.add(
        "the rendered page contains the whole document",
        not lossy and not too_short,
        (
            f"lossy wrap: {lossy[:3]}"
            if lossy
            else (f"page too short: {too_short[:3]}" if too_short else "nothing clipped")
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", default="eval/synthetic_docs", help="corpus directory")
    parser.add_argument("--count", type=int, default=200, help="expected document count")
    parser.add_argument("--seed", type=int, default=20261019, help="the generator's seed")
    parser.add_argument("--today", default="2026-10-20", help="the reference date")
    args = parser.parse_args(argv)

    docs_dir = Path(args.docs)
    if not docs_dir.exists():
        print(f"{docs_dir} does not exist — run: make synth", file=sys.stderr)
        return 1

    records = load_corpus(docs_dir)
    report = Report()
    full_coverage = len(records) >= FULL_COVERAGE_MIN

    report.add(
        f"corpus has {args.count} documents",
        len(records) == args.count,
        f"found {len(records)}",
    )

    kinds = {record.get("kind") for record in records}
    missing_kinds = set(EXPECTED_KINDS) - kinds
    if missing_kinds and not full_coverage:
        report.add(
            "every document kind is represented",
            True,
            f"not asserted on a corpus of {len(records)} — missing {sorted(missing_kinds)}",
            skipped=True,
        )
    else:
        report.add(
            "every document kind is represented",
            not missing_kinds,
            f"missing {sorted(missing_kinds)}"
            if missing_kinds
            else ", ".join(sorted(kinds)),
        )

    check_formats(records, report, full_coverage)
    check_dates(records, report, full_coverage)
    check_duplicates(records, report)
    check_ground_truth(docs_dir, records, report)
    check_images(docs_dir, records, report)
    check_no_clipping(docs_dir, records, report)
    check_reproducible(docs_dir, report, args.seed, args.today, len(records))

    print(report.render())
    print()
    if report.ok:
        print(f"corpus OK — {len(records)} documents")
        return 0
    print(f"corpus FAILED — {sum(1 for c in report.checks if not c.ok)} check(s) failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
