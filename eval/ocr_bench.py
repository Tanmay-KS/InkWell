#!/usr/bin/env python3
"""Compare candidate OCR / vision models on the synthetic corpus.

Phase 0 — Person B.

The plan requires an answer to one question before Phase 1 starts: *which
model do we use to read documents?* This script produces the table that
answers it.

Candidates:

  * `nvidia/NVIDIA-Nemotron-3-Nano-Omni` — the Nemotron family, omni-modal
  * `Qwen/Qwen2.5-VL-72B-Instruct` — the model Nebius's own
    document-intelligence page names for layout analysis, table extraction
    and OCR
  * `local-tesseract` — the open-source fallback, running inside the
    sandbox, with no egress at all
  * `local-text` — the trivial baseline: the document was already text

Scoring is field recovery, not string similarity. For each document we know
the ground truth (the manifest written by `gen_synthetic_docs.py`), so we
ask: did the extracted text contain the renewal date, the account number,
the provider, the holder, and the amount? A model that transcribes a page
beautifully but mangles the account number is not good enough for us, and
an edit-distance metric would not notice.

Usage:

    python eval/ocr_bench.py --docs eval/synthetic_docs --count 10
    python eval/ocr_bench.py --docs eval/synthetic_docs --count 10 --json results.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TOKEN_FACTORY_BASE_URL = os.environ.get(
    "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"
).rstrip("/")

DEFAULT_CANDIDATES = [
    "nvidia/NVIDIA-Nemotron-3-Nano-Omni",
    "Qwen/Qwen2.5-VL-72B-Instruct",
]


@dataclass
class CandidateResult:
    """The outcome of running one candidate over the sample."""

    name: str
    kind: str  # "vision" | "local" | "text"
    docs: int = 0
    fields_found: int = 0
    fields_total: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        if self.fields_total == 0:
            return 0.0
        return self.fields_found / self.fields_total

    @property
    def mean_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = max(0, int(len(ordered) * 0.95) - 1)
        return ordered[index]


# --- helpers ----------------------------------------------------------------


def normalise(value: str) -> str:
    """Lowercase and drop everything that is not a letter or digit.

    Account numbers appear as `1234-5678`, `1234 5678` and `12345678` in
    different documents. Scoring has to treat them as the same value.
    """
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _image_data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


PROMPT = (
    "Transcribe this document exactly. Output only the document text, "
    "preserving every number, date, name, and reference code verbatim. "
    "Do not summarise, do not comment, do not add a preamble."
)


def run_vision_model(
    model: str, image_path: Path, api_key: str, timeout: float
) -> tuple[str, int, int]:
    """Call a vision model on Token Factory.

    Returns:
        (text, prompt_tokens, completion_tokens)

    Raises:
        RuntimeError on an API or transport failure.
    """
    import httpx

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": _image_data_uri(image_path)},
                    },
                ],
            }
        ],
        "max_tokens": 2048,
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
    text = data["choices"][0]["message"].get("content") or ""
    usage = data.get("usage", {})
    return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def run_tesseract(image_path: Path, timeout: float) -> str:
    """Run tesseract locally. Returns the extracted text.

    This candidate has no egress at all, which is why it stays in the
    comparison even when it loses on accuracy: it is the floor the
    privacy story rests on.
    """
    if shutil.which("tesseract") is None:
        raise RuntimeError("tesseract is not installed")
    proc = subprocess.run(
        ["tesseract", str(image_path), "stdout"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract exited {proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout


# --- scoring ----------------------------------------------------------------

# The fields whose recovery actually matters downstream. The date and the
# account number are the two the pipeline cannot work without.
SCORED_FIELDS = [
    "renewal_date",
    "account_number",
    "provider",
    "holder",
    "amount",
]


def score_document(truth: dict, extracted: str) -> tuple[int, int]:
    """Count how many ground-truth fields appear in the extracted text.

    Returns:
        (fields_found, fields_total)
    """
    haystack = normalise(extracted)
    found = 0
    total = 0

    for field_name in SCORED_FIELDS:
        value = truth.get(field_name)
        if not value:
            continue
        total += 1
        # Amounts are compared on their digits only; the currency symbol
        # and the thousands separator vary.
        needle = normalise(str(value))
        if field_name == "amount":
            digits = re.sub(r"[^0-9.]", "", str(value))
            if digits and digits in str(value).replace(",", ""):
                found += 1
                continue
        if needle and needle in haystack:
            found += 1

    return found, total


# --- candidates -------------------------------------------------------------


def evaluate(
    docs_dir: Path,
    count: int,
    api_key: str,
    timeout: float,
    candidates: list[str],
    use_images: bool,
) -> list[CandidateResult]:
    """Run every available candidate over the sample and score it."""
    truth_files = sorted(docs_dir.glob("doc-*.json"))
    if not truth_files:
        print(f"no documents found in {docs_dir}", file=sys.stderr)
        print("run: make synth", file=sys.stderr)
        return []

    sample = truth_files[:count]
    results: list[CandidateResult] = []

    # --- vision models ---
    if api_key:
        for model in candidates:
            result = CandidateResult(name=model, kind="vision")
            for truth_path in sample:
                doc_id = truth_path.stem
                image_path = docs_dir / f"{doc_id}.png"
                text_path = docs_dir / f"{doc_id}.txt"

                if use_images and image_path.exists():
                    target: Path = image_path
                elif text_path.exists():
                    target = text_path
                else:
                    continue

                try:
                    started = time.perf_counter()
                    extracted, _prompt_tokens, _completion_tokens = run_vision_model(
                        model, target, api_key, timeout
                    )
                    result.latencies.append(time.perf_counter() - started)
                except Exception as exc:
                    result.errors.append(f"{doc_id}: {exc}")
                    continue

                truth = json.loads(truth_path.read_text(encoding="utf-8"))
                found, total = score_document(truth, extracted)
                result.fields_found += found
                result.fields_total += total
                result.docs += 1

            results.append(result)
    else:
        print(
            "NEBIUS_API_KEY is not set — skipping the vision-model candidates.",
            file=sys.stderr,
        )

    # --- local tesseract ---
    if use_images and shutil.which("tesseract"):
        result = CandidateResult(name="local-tesseract", kind="local")
        for truth_path in sample:
            doc_id = truth_path.stem
            image_path = docs_dir / f"{doc_id}.png"
            if not image_path.exists():
                continue
            try:
                started = time.perf_counter()
                extracted = run_tesseract(image_path, timeout)
                result.latencies.append(time.perf_counter() - started)
            except Exception as exc:
                result.errors.append(f"{doc_id}: {exc}")
                continue
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            found, total = score_document(truth, extracted)
            result.fields_found += found
            result.fields_total += total
            result.docs += 1
        results.append(result)
    elif use_images:
        print(
            "tesseract is not installed — skipping the local candidate.\n"
            "         (it is baked into the sandbox image: sandbox/Dockerfile.hermes)",
            file=sys.stderr,
        )

    # --- trivial baseline ---
    result = CandidateResult(name="local-text (baseline)", kind="text")
    for truth_path in sample:
        text_path = docs_dir / f"{truth_path.stem}.txt"
        if not text_path.exists():
            continue
        extracted = text_path.read_text(encoding="utf-8")
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        found, total = score_document(truth, extracted)
        result.fields_found += found
        result.fields_total += total
        result.docs += 1
    results.append(result)

    return results


def render_table(results: list[CandidateResult]) -> str:
    """Render the comparison as a markdown table."""
    if not results:
        return "_no candidates were evaluated_"

    lines = [
        "| Candidate | Kind | Docs | Field recovery | Mean latency | p95 latency | Errors |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| `{r.name}` | {r.kind} | {r.docs} | "
            f"{r.accuracy * 100:.1f}% | {r.mean_latency:.2f}s | "
            f"{r.p95_latency:.2f}s | {len(r.errors)} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", default="eval/synthetic_docs", help="corpus directory")
    parser.add_argument("--count", type=int, default=10, help="documents to sample")
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_CANDIDATES),
        help="comma-separated Token Factory model IDs to compare",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout")
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="send the .txt file instead of the rendered .png",
    )
    parser.add_argument("--json", dest="json_out", default="", help="write raw results here")
    args = parser.parse_args(argv)

    api_key = os.environ.get("NEBIUS_API_KEY", "")
    if not api_key:
        # .env is not read automatically here; the Makefile loads it.
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("NEBIUS_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip("'\"")
                    break

    docs_dir = Path(args.docs)
    use_images = not args.text_only and any(docs_dir.glob("doc-*.png"))

    if not use_images and not args.text_only:
        print(
            "no PNGs found — evaluating against the text files.\n"
            "Regenerate with images: make synth  (needs Pillow)",
            file=sys.stderr,
        )

    candidates = [m.strip() for m in args.models.split(",") if m.strip()]

    print(f"evaluating {args.count} documents from {docs_dir}\n")
    results = evaluate(
        docs_dir, args.count, api_key, args.timeout, candidates, use_images
    )

    table = render_table(results)
    print(table)

    for r in results:
        if r.errors:
            print(f"\n{r.name}: {len(r.errors)} error(s), first three:", file=sys.stderr)
            for err in r.errors[:3]:
                print(f"  {err}", file=sys.stderr)

    if args.json_out:
        payload = [
            {
                "name": r.name,
                "kind": r.kind,
                "docs": r.docs,
                "fields_found": r.fields_found,
                "fields_total": r.fields_total,
                "accuracy": r.accuracy,
                "mean_latency": r.mean_latency,
                "p95_latency": r.p95_latency,
                "errors": r.errors,
            }
            for r in results
        ]
        Path(args.json_out).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
