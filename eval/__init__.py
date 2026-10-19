"""The three proofs.

Phase 2-3 — Person B (leak test, memory eval) and Person A (cost bench).

    gen_synthetic_docs.py     the corpus generator
    leak_test.py              router ON vs OFF
    memory_updates_eval.py    memory-with-updates accuracy
    cost_latency_bench.py     per-tier cost and latency

The corpus is generated, not committed: `make synth` reproduces it exactly
from a fixed seed.
"""

__all__ = [
    "cost_latency_bench",
    "gen_synthetic_docs",
    "leak_test",
    "memory_updates_eval",
]
