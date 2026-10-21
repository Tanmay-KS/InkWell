#!/usr/bin/env python3
"""Compare candidate OCR / vision models on the synthetic corpus.

Phase 0 — Person B.

The Phase 0 decision is that **`nvidia/Nemotron-3.5-Super-VL`** reads our
documents. This script produces the evidence behind that decision and the
numbers the rest of the project depends on:

* can each candidate recover the fields the pipeline needs?
* what does it cost, and how slow is it?
* how good is the **local, zero-egress** fallback? That number decides
  whether the pipeline can extract locally and redact before the model
  ever sees a document — which is the only way the project's headline
  privacy claim stays literally true.

Scoring is **field recovery**, not string similarity — see `scoring.py`.

Candidates:

  * `nvidia/Nemotron-3.5-Super-VL` — the decided document-reading model
  * other vision models from the live catalog, for comparison
  * `local-tesseract` — the open-source fallback, zero egress
  * `local-rapidocr` — a pip-only local fallback, zero egress
  * `local-text` — the trivial baseline: the document was already text

Two rules this script will not bend:

1. **A vision model is never sent a text file.** An earlier version
   base64-encoded `.txt` files and posted them as `data:image/png`, which
   every candidate rejected with an opaque HTTP 400. The bench now
   refuses to do that, and a candidate that cannot run is reported as
   `SKIPPED` with the reason rather than silently vanishing or scoring
   a meaningless zero.
2. **A candidate that cannot run is still a row.** A missing row reads as
   "untested" rather than "unavailable", and a decision made on a missing
   row is a decision made blind.

Usage:

    python eval/ocr_bench.py --docs eval/synthetic_docs --count 10
    python eval/ocr_bench.py --docs eval/synthetic_docs --count 10 --json results.json
    python eval/ocr_bench.py --from-catalog --count 10
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.scoring import (
    SCORED_FIELDS,
    AggregateScore,
    line_coverage,
    render_field_table,
    score_document,
)
from eval.tokenfactory import (
    accepts_images,
    fetch_models,
    find_model,
    load_api_key,
    pricing_per_token,
)

TOKEN_FACTORY_BASE_URL = os.environ.get(
    "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"
).rstrip("/")

# The decided model comes first. The others are comparators: the question
# they answer is "is Super-VL actually the right pick, and by how much".
DEFAULT_CANDIDATES = [
    "nvidia/Nemotron-3.5-Super-VL",
]

COMPARATOR_CANDIDATES = [
    "nvidia/Nemotron-3.5-Super-VL",
    "zai-org/GLM-5.3-Flash",
    "deepseek-ai/DeepSeek-V4.1-Flash",
    "Qwen/Qwen3.8-27B",
    "openbmb/MiniCPM-V-4_5",
]

PROMPT = (
    "Transcribe this document exactly. Output only the document text, "
    "preserving every number, date, name, and reference code verbatim. "
    "Do not summarise, do not comment, do not add a preamble."
)


@dataclass
class CandidateResult:
    """The outcome of running one candidate over the sample."""

    name: str
    kind: str  # "vision" | "local" | "text"
    status: str = "ok"  # "ok" | "skipped"
    skip_reason: str = ""
    docs: int = 0
    aggregate: AggregateScore = field(default_factory=AggregateScore)
    latencies: list[float] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    coverage: list[float] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.aggregate.accuracy

    @property
    def mean_coverage(self) -> float:
        """How much of each source document survived the extraction.

        Field recovery is forgiving about spacing; this is not. A line
        that came back not at all leaves a hole in the document, and no
        field score would say so.
        """
        return statistics.mean(self.coverage) if self.coverage else 0.0

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

    @property
    def cost_usd(self) -> float:
        return self.prompt_tokens * self._prompt_rate + self.completion_tokens * self._completion_rate

    _prompt_rate: float = 0.0
    _completion_rate: float = 0.0


# --- helpers ----------------------------------------------------------------


def _image_data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def is_image(path: Path) -> bool:
    """Whether a file is really an image, by magic bytes.

    This is the guard that stops a `.txt` file being posted as an image.
    The extension is not enough: the corpus writes `.txt` and `.png` side
    by side, and a mistake there produces a benchmark that looks like it
    ran but measured nothing.
    """
    try:
        header = path.read_bytes()[:8]
    except OSError:
        return False
    return header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")


# --- candidates -------------------------------------------------------------


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
    """Run the tesseract binary. Returns the extracted text."""
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


_RAPIDOCR_ENGINE = None


def run_rapidocr(image_path: Path, timeout: float) -> str:
    """Run RapidOCR (ONNX, pip-only). Returns the extracted text.

    This is the local fallback for a machine that cannot install system
    packages. It has no egress either, which is the property that matters
    for the privacy story.
    """
    global _RAPIDOCR_ENGINE

    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError(f"rapidocr-onnxruntime is not installed: {exc}") from exc

    if _RAPIDOCR_ENGINE is None:
        _RAPIDOCR_ENGINE = RapidOCR()

    result, _elapse = _RAPIDOCR_ENGINE(str(image_path))
    if not result:
        return ""
    return "\n".join(str(line[1]) for line in result)


# --- evaluation -------------------------------------------------------------


def evaluate(
    docs_dir: Path,
    count: int,
    api_key: str,
    timeout: float,
    candidates: list[str],
    use_images: bool,
    local_backends: list[str],
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
            if not use_images:
                result.status = "skipped"
                result.skip_reason = "no rendered images — run `make synth`"
                results.append(result)
                continue

            model_info = None
            try:
                catalog = fetch_models(api_key)
                model_info = find_model(catalog, model)
                if model_info is None:
                    result.status = "skipped"
                    result.skip_reason = "not served by Token Factory"
                    results.append(result)
                    continue
                prompt_rate, completion_rate = pricing_per_token(model_info)
                result._prompt_rate = prompt_rate
                result._completion_rate = completion_rate
            except RuntimeError:
                # No catalog is not a reason to skip the model — pricing
                # is nice to have, the run is the point.
                pass

            for truth_path in sample:
                doc_id = truth_path.stem
                image_path = docs_dir / f"{doc_id}.png"

                if not image_path.exists():
                    result.status = "skipped"
                    result.skip_reason = f"no image for {doc_id}"
                    break
                if not is_image(image_path):
                    result.status = "skipped"
                    result.skip_reason = f"{doc_id}.png is not an image — refusing to send it"
                    break

                try:
                    started = time.perf_counter()
                    extracted, prompt_tokens, completion_tokens = run_vision_model(
                        model, image_path, api_key, timeout
                    )
                    result.latencies.append(time.perf_counter() - started)
                    result.prompt_tokens += prompt_tokens
                    result.completion_tokens += completion_tokens
                except Exception as exc:
                    result.errors.append(f"{doc_id}: {exc}")
                    continue

                truth = json.loads(truth_path.read_text(encoding="utf-8"))
                result.aggregate.add(score_document(truth, extracted))
                result.coverage.append(_coverage_of(docs_dir, doc_id, extracted))
                result.docs += 1

            if result.errors and result.docs == 0:
                result.status = "skipped"
                result.skip_reason = f"all calls failed — {result.errors[0]}"

            results.append(result)
    else:
        print(
            "NEBIUS_API_KEY is not set — the vision-model candidates are SKIPPED.",
            file=sys.stderr,
        )
        for model in candidates:
            result = CandidateResult(name=model, kind="vision")
            result.status = "skipped"
            result.skip_reason = "NEBIUS_API_KEY is not set"
            results.append(result)

    # --- local, zero-egress candidates ---
    local_runners = {
        "tesseract": ("local-tesseract", run_tesseract),
        "rapidocr": ("local-rapidocr", run_rapidocr),
    }

    for backend in local_backends:
        label, runner = local_runners[backend]
        result = CandidateResult(name=label, kind="local")

        if not use_images:
            result.status = "skipped"
            result.skip_reason = "no rendered images — run `make synth`"
            results.append(result)
            continue

        for truth_path in sample:
            doc_id = truth_path.stem
            image_path = docs_dir / f"{doc_id}.png"
            if not image_path.exists() or not is_image(image_path):
                result.status = "skipped"
                result.skip_reason = f"no usable image for {doc_id}"
                break
            try:
                started = time.perf_counter()
                extracted = runner(image_path, timeout)
                result.latencies.append(time.perf_counter() - started)
            except Exception as exc:
                result.errors.append(f"{doc_id}: {exc}")
                continue
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            result.aggregate.add(score_document(truth, extracted))
            result.coverage.append(_coverage_of(docs_dir, truth_path.stem, extracted))
            result.docs += 1

        if result.errors and result.docs == 0:
            result.status = "skipped"
            result.skip_reason = f"all calls failed — {result.errors[0]}"

        results.append(result)

    # --- trivial baseline ---
    result = CandidateResult(name="local-text (baseline)", kind="text")
    for truth_path in sample:
        text_path = docs_dir / f"{truth_path.stem}.txt"
        if not text_path.exists():
            continue
        extracted = text_path.read_text(encoding="utf-8")
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        result.aggregate.add(score_document(truth, extracted))
        result.coverage.append(1.0)
        result.docs += 1
    results.append(result)

    return results


def _coverage_of(docs_dir: Path, doc_id: str, extracted: str) -> float:
    """How much of the source document survived this extraction."""
    source_path = docs_dir / f"{doc_id}.txt"
    if not source_path.exists():
        return 0.0
    return line_coverage(source_path.read_text(encoding="utf-8"), extracted)


def render_table(results: list[CandidateResult]) -> str:
    """Render the comparison as a markdown table."""
    if not results:
        return "_no candidates were evaluated_"

    lines = [
        "| Candidate | Kind | Status | Docs | Field recovery | Line coverage | Mean | p95 | Tokens (in/out) | Cost |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for r in results:
        status = "ok" if r.status == "ok" else f"SKIPPED — {r.skip_reason}"
        tokens = f"{r.prompt_tokens:,} / {r.completion_tokens:,}" if r.kind == "vision" else "—"
        cost = f"${r.cost_usd:.4f}" if r.kind == "vision" else "—"
        lines.append(
            f"| `{r.name}` | {r.kind} | {status} | {r.docs} | "
            f"{r.accuracy * 100:.1f}% | {r.mean_coverage * 100:.1f}% | "
            f"{r.mean_latency:.2f}s | "
            f"{r.p95_latency:.2f}s | {tokens} | {cost} |"
        )
    return "\n".join(lines)


def render_field_breakdown(results: list[CandidateResult]) -> str:
    """Render the per-field recovery table."""
    lines = ["| Candidate | " + " | ".join(f"`{f}`" for f in SCORED_FIELDS) + " |"]
    lines.append("|---|" + "---:|" * len(SCORED_FIELDS))
    for r in results:
        if r.status != "ok":
            continue
        cells = []
        for f in SCORED_FIELDS:
            total = r.aggregate.per_field_total.get(f, 0)
            found = r.aggregate.per_field_found.get(f, 0)
            cells.append(f"{found / total * 100:.0f}%" if total else "—")
        lines.append(f"| `{r.name}` | " + " | ".join(cells) + " |")
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
    parser.add_argument(
        "--comparators",
        action="store_true",
        help="also run the comparison set of vision models",
    )
    parser.add_argument(
        "--from-catalog",
        action="store_true",
        help="run every vision model the catalog serves",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout")
    parser.add_argument(
        "--local",
        default="tesseract,rapidocr",
        help="comma-separated local OCR backends to try, in order",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="evaluate against the .txt files; vision and local candidates are SKIPPED",
    )
    parser.add_argument("--json", dest="json_out", default="", help="write raw results here")
    args = parser.parse_args(argv)

    api_key = load_api_key()

    docs_dir = Path(args.docs)
    pngs = sorted(docs_dir.glob("doc-*.png"))
    use_images = not args.text_only and bool(pngs)

    if not use_images and not args.text_only:
        print(
            "no PNGs found — the vision and local candidates will be SKIPPED.\n"
            "Regenerate with images: make synth  (needs Pillow)",
            file=sys.stderr,
        )
    elif not args.text_only:
        print(f"{len(pngs)} rendered images available\n")

    candidates = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.comparators:
        for model in COMPARATOR_CANDIDATES:
            if model not in candidates:
                candidates.append(model)
    if args.from_catalog:
        if not api_key:
            print("--from-catalog needs NEBIUS_API_KEY", file=sys.stderr)
            return 1
        catalog = fetch_models(api_key)
        for model in catalog:
            if accepts_images(model) and model["id"] not in candidates:
                candidates.append(model["id"])

    local_backends = [b.strip() for b in args.local.split(",") if b.strip()]

    print(f"evaluating {args.count} documents from {docs_dir}\n")
    results = evaluate(
        docs_dir, args.count, api_key, args.timeout, candidates, use_images, local_backends
    )

    print(render_table(results))
    print("\n### Field recovery\n")
    print(render_field_breakdown(results))

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
                "status": r.status,
                "skip_reason": r.skip_reason,
                "docs": r.docs,
                "fields_found": r.aggregate.fields_found,
                "fields_total": r.aggregate.fields_total,
                "accuracy": r.accuracy,
                "line_coverage": r.mean_coverage,
                "per_field": {
                    field_name: {
                        "found": r.aggregate.per_field_found.get(field_name, 0),
                        "total": r.aggregate.per_field_total.get(field_name, 0),
                    }
                    for field_name in SCORED_FIELDS
                },
                "mean_latency": r.mean_latency,
                "p95_latency": r.p95_latency,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cost_usd": r.cost_usd,
                "errors": r.errors,
            }
            for r in results
        ]
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
