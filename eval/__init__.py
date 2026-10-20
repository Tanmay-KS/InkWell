"""The three proofs.

Phase 2-3 — Person B (leak test, memory eval) and Person A (cost bench).

    gen_synthetic_docs.py       the corpus generator
    qa_corpus.py                corpus validation
    corpus_redaction_audit.py   what the deterministic redactor misses
    ocr_bench.py                which model reads documents
    redacted_extract_bench.py   can a model read *redacted* text
    scoring.py                  field-recovery scoring, shared by the benches
    tokenfactory.py             the live model catalog
    pin_models.py               pins model IDs into .env
    leak_test.py                router ON vs OFF
    memory_updates_eval.py      memory-with-updates accuracy
    cost_latency_bench.py       per-tier cost and latency

The corpus is generated, not committed: `make synth` reproduces it exactly
from a fixed seed and a fixed reference date.
"""

__all__ = [
    "corpus_redaction_audit",
    "cost_latency_bench",
    "gen_synthetic_docs",
    "leak_test",
    "memory_updates_eval",
    "ocr_bench",
    "pin_models",
    "qa_corpus",
    "scoring",
    "tokenfactory",
]
