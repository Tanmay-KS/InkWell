"""Tests for the synthetic corpus generator.

Phase 0 — Person B.

The corpus is the raw material for the Phase 2 leak test and the Phase 3
evaluations, and it is generated rather than committed. Two properties
have to hold, and both were broken at some point:

* **It is reproducible.** The same seed must give the same corpus. That
  means the reference date has to be pinned too — the generator derives
  every renewal date from it, so an unpinned date means "the same seed"
  gives a different corpus on a different day.
* **The image contains the whole document.** The renderer used to
  truncate long lines at the right edge of the page and drop lines past
  the bottom, so the rendered "scan" was a partial document and every
  OCR candidate was judged on a page missing text.

These tests generate their own small corpus, so they run in CI without
the full one being present.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eval.gen_synthetic_docs import generate_corpus
from eval.scoring import normalise_alnum, score_document

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL = REPO_ROOT / "eval"

SEED = 20261019
TODAY = "2026-10-20"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Path:
    """A small corpus, generated once for the module."""
    out = tmp_path_factory.mktemp("corpus")
    generate_corpus(out, count=12, seed=SEED, today=TODAY, render_images=True)
    return out


def test_the_corpus_is_reproducible(tmp_path):
    """The same seed and the same reference date give the same corpus."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_corpus(first, count=6, seed=SEED, today=TODAY, render_images=False)
    generate_corpus(second, count=6, seed=SEED, today=TODAY, render_images=False)

    for path in sorted(first.glob("doc-*")):
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_a_different_seed_gives_a_different_corpus(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_corpus(first, count=6, seed=SEED, today=TODAY, render_images=False)
    generate_corpus(second, count=6, seed=SEED + 1, today=TODAY, render_images=False)

    first_text = "".join(sorted(p.read_text() for p in first.glob("doc-*.txt")))
    second_text = "".join(sorted(p.read_text() for p in second.glob("doc-*.txt")))
    assert first_text != second_text


def test_the_reference_date_is_part_of_the_contract(tmp_path):
    """Without a pinned date the corpus is not reproducible."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_corpus(first, count=6, seed=SEED, today=TODAY, render_images=False)
    generate_corpus(second, count=6, seed=SEED, today="2026-10-21", render_images=False)

    first_text = "".join(sorted(p.read_text() for p in first.glob("doc-*.txt")))
    second_text = "".join(sorted(p.read_text() for p in second.glob("doc-*.txt")))
    assert first_text != second_text


def test_the_ground_truth_matches_the_document(corpus: Path):
    """Every value the scorer uses must really be in the document."""
    for truth_path in sorted(corpus.glob("doc-*.json")):
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        text = (corpus / f"{truth_path.stem}.txt").read_text(encoding="utf-8")
        normalised = normalise_alnum(text)
        for field_name in ("holder", "email", "phone", "account_number", "provider"):
            assert normalise_alnum(str(truth[field_name])) in normalised, (
                f"{truth_path.stem}.{field_name} is not in the document"
            )


def test_a_document_scored_against_its_own_text_is_perfect(corpus: Path):
    """The scorer's invariant, on real generated documents."""
    for truth_path in sorted(corpus.glob("doc-*.json")):
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        text = (corpus / f"{truth_path.stem}.txt").read_text(encoding="utf-8")
        score = score_document(truth, text)
        assert score.found == score.total, (truth_path.stem, score.matched)


def test_the_rendered_page_contains_the_whole_document(corpus: Path):
    """Nothing is clipped off the right edge or dropped off the bottom."""
    from PIL import Image

    from eval.gen_synthetic_docs import LINE_HEIGHT, MARGIN, wrap_lines

    for truth_path in sorted(corpus.glob("doc-*.json")):
        text = (corpus / f"{truth_path.stem}.txt").read_text(encoding="utf-8")
        image_path = corpus / f"{truth_path.stem}.png"

        assert image_path.exists(), f"{truth_path.stem} has no image"

        wrapped = wrap_lines(text)
        rebuilt = " ".join(wrapped).split()
        original = text.split()
        assert rebuilt == original, f"{truth_path.stem} lost text when wrapped"

        needed = MARGIN * 2 + len(wrapped) * LINE_HEIGHT
        with Image.open(image_path) as image:
            assert image.size[1] >= needed, f"{truth_path.stem} is too short"


def test_the_manifest_matches_the_documents(corpus: Path):
    manifest = [json.loads(line) for line in (corpus / "manifest.jsonl").read_text().splitlines()]
    assert len(manifest) == 12
    assert {record["doc_id"] for record in manifest} == {
        path.stem for path in corpus.glob("doc-*.json")
    }


def test_the_generator_can_be_run_as_a_script(tmp_path):
    """The Makefile calls it as a script; the import path must work."""
    proc = subprocess.run(
        [
            sys.executable,
            str(EVAL / "gen_synthetic_docs.py"),
            "--out",
            str(tmp_path),
            "--count",
            "2",
            "--seed",
            str(SEED),
            "--today",
            TODAY,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(list(tmp_path.glob("doc-*.txt"))) == 2
