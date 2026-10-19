#!/usr/bin/env python3
"""The leak test — router ON vs OFF.

Phase 2 — Person B.

This is the project's headline number. The question it answers: *when the
agent goes looking for something on the web, how often does a personal
detail leave the sandbox?*

The method is deliberately blunt, because a number that cannot be argued
with is worth more than a number that is merely sophisticated.

1. Run every synthetic document through the pipeline.
2. Capture every outbound request the pipeline would make — the Tavily
   query, the model prompt, any tool call.
3. Score each captured payload for personal detail, using
   `privacy_router.redact.contains_personal_detail`.
4. Repeat with the router attached (ON) and not attached (OFF).
5. Report the rate in each condition, and the reduction.

**The router ON/OFF toggle is the presence of the `network_middlewares`
entry in the sandbox policy** — not a flag inside our code. That is the
point: the ON condition has to be the real enforcement path, or the
measurement is of a toy.

What the number means, stated precisely:

* OFF — the fraction of outbound payloads containing a detectable personal
  detail, with no router in the path.
* ON — the same fraction, with the router in the path.
* The claim we can make is "personal details never reach a Super/Ultra
  tier or a third-party service".

**What the number does not cover, and must not hide:** the redaction
model call is itself outbound traffic carrying the source text. It is
reported as its own line item, never folded into the headline. The
architectural defence for that is the ordering in
`privacy_router/redact.py` — the deterministic local pass runs first, and
only spans it cannot classify are escalated to a model.

Phase 2 implementation.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["LeakStats", "main", "run_leak_test"]


@dataclass
class LeakStats:
    """The result of one condition."""

    condition: str  # "router_off" | "router_on"
    payloads: int = 0
    payloads_with_pii: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    examples: list[dict[str, str]] = field(default_factory=list)

    @property
    def leak_rate(self) -> float:
        """The fraction of outbound payloads containing personal detail."""
        if self.payloads == 0:
            return 0.0
        return self.payloads_with_pii / self.payloads

    def to_dict(self) -> dict:
        return {
            "condition": self.condition,
            "payloads": self.payloads,
            "payloads_with_pii": self.payloads_with_pii,
            "leak_rate": round(self.leak_rate, 4),
            "categories": self.categories,
        }


def run_leak_test(
    docs_dir: str | Path,
    *,
    conditions: tuple[str, ...] = ("router_off", "router_on"),
) -> dict[str, LeakStats]:
    """Run the leak test.

    Args:
        docs_dir: The corpus directory.
        conditions: Which conditions to measure.

    Returns:
        A mapping of condition name to its statistics.

    Phase 2 implementation.
    """
    raise NotImplementedError("Phase 2 — see IMPLEMENTATION.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", default="eval/synthetic_docs")
    parser.add_argument("--json", dest="json_out", default="eval/results/leak_test.json")
    args = parser.parse_args(argv)

    stats = run_leak_test(args.docs)

    print("| Condition | Payloads | With personal detail | Leak rate |")
    print("|---|---:|---:|---:|")
    for name, s in stats.items():
        print(
            f"| {name} | {s.payloads} | {s.payloads_with_pii} | "
            f"{s.leak_rate * 100:.1f}% |"
        )

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({k: v.to_dict() for k, v in stats.items()}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
