#!/usr/bin/env python3
"""The leak test — router ON vs OFF.

Phase 2 — Person B.

This is the project's headline number. The question it answers: *when the
agent goes looking for something on the web, how often does a personal
detail leave the sandbox?*

The method is deliberately blunt, because a number that cannot be argued
with is worth more than a number that is merely sophisticated.

1. Run every synthetic document through the pipeline.
2. Capture every outbound request the pipeline would make — the model
   prompt, the Tavily query, the vision fallback.
3. Score each captured payload for personal detail, using
   `privacy_router.redact.contains_personal_detail`.
4. Repeat with the router attached (ON) and not attached (OFF).
5. Report the rate in each condition, and the reduction.

**No request is sent.** Every call is captured at the egress seam
(`privacy_router/egress.py`), so the measurement costs nothing, needs no
API key, and gives the same answer on every run. The number is about what
*would* leave, which is the question — what the far end would answer is
irrelevant to it.

**The router ON/OFF toggle in the sandbox is the presence of the
`network_middlewares` entry in the sandbox policy** — not a flag inside our
code. That is the point: the ON condition has to be the real enforcement
path, or the measurement is of a toy. This run measures **the redaction
layer the middleware invokes**, which is the same `privacy_router.redact`.
It is the router's coverage, not proof that the gateway applied it; the
gateway ON leg needs the sandbox host and is reported separately (see
`docs/leak-test.md`).

What the number means, stated precisely:

* OFF — the fraction of outbound payloads containing a detectable personal
  detail, with no router in the path. The same pipeline, the same
  documents, the same query — only the privacy layer removed.
* ON — the same fraction, with `privacy_router.redact` in the path.
* The claim we can make is "personal details never reach a Super/Ultra
  tier or a third-party service through an outbound payload".

**What the number does not cover, and must not hide:** the redaction model
call is itself outbound traffic carrying the source text. It is reported
as its own line item, never folded into the headline. The architectural
defence for that is the ordering in `privacy_router/redact.py` — the
deterministic local pass runs first, and the escalation carries the text
*after* that pass.

An `ocr_call` is scored as a leak whenever it happens, because its payload
is the document image and an image is personal by definition. On this
corpus the local extractor reads every document, so no image is sent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.pipeline import process_document
from privacy_router.egress import (
    OutboundRequest,
    capture,
    capturing_transport,
)
from privacy_router.redact import contains_personal_detail, redact
from search.tavily_client import TavilyClient, build_query

__all__ = ["ConditionResult", "LeakStats", "main", "run_leak_test"]

# The corpus is generated against a fixed date, so the test has to use the
# same one or the deadlines move and the queries change with them.
CORPUS_TODAY = date(2026, 10, 20)

# The calls that carry user data to a model or a third party. These are the
# headline.
HEADLINE_KINDS = ("ocr_call", "lookup_call")

# Every line item the harness knows how to measure. `redact_call` is here
# and deliberately not in the headline.
LINE_ITEM_KINDS = ("ocr_call", "lookup_call", "redact_call")

# A canned answer for the capturing transports. Nothing is sent, and the
# answers only have to be well-formed enough for the caller to continue.
EMPTY_SEARCH = '{"results": []}'
NO_RESIDUAL_SPANS = '{"spans": []}'


@dataclass(slots=True)
class LeakStats:
    """One condition × one line item."""

    condition: str  # "router_off" | "router_on"
    kind: str  # "ocr_call" | "lookup_call" | "redact_call" | "headline"
    payloads: int = 0
    payloads_with_pii: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    examples: list[dict[str, str]] = field(default_factory=list)
    note: str = ""

    @property
    def leak_rate(self) -> float:
        """The fraction of outbound payloads containing personal detail."""
        if self.payloads == 0:
            return 0.0
        return self.payloads_with_pii / self.payloads

    def add(self, found: list[str], *, payload: str) -> None:
        """Record one captured payload and the categories found in it."""
        self.payloads += 1
        if not found:
            return
        self.payloads_with_pii += 1
        for category in found:
            self.categories[category] = self.categories.get(category, 0) + 1
        if len(self.examples) < 3:
            self.examples.append({"categories": ", ".join(sorted(set(found))), "excerpt": _excerpt(payload)})

    def to_dict(self) -> dict:
        return {
            "condition": self.condition,
            "kind": self.kind,
            "payloads": self.payloads,
            "payloads_with_pii": self.payloads_with_pii,
            "leak_rate": round(self.leak_rate, 4),
            "categories": dict(sorted(self.categories.items())),
            "examples": self.examples,
            "note": self.note,
        }


@dataclass(slots=True)
class ConditionResult:
    """Everything one condition measured."""

    condition: str
    documents: int = 0
    line_items: dict[str, LeakStats] = field(default_factory=dict)

    def stats(self, kind: str) -> LeakStats:
        return self.line_items.setdefault(kind, LeakStats(condition=self.condition, kind=kind))

    @property
    def headline(self) -> LeakStats:
        """The two calls that carry user data out, merged."""
        merged = LeakStats(condition=self.condition, kind="headline")
        for kind in HEADLINE_KINDS:
            item = self.line_items.get(kind)
            if item is None:
                continue
            merged.payloads += item.payloads
            merged.payloads_with_pii += item.payloads_with_pii
            for category, count in item.categories.items():
                merged.categories[category] = merged.categories.get(category, 0) + count
            merged.examples.extend(item.examples[:1])
        return merged

    def to_dict(self) -> dict:
        return {
            "condition": self.condition,
            "documents": self.documents,
            "headline": self.headline.to_dict(),
            "line_items": {kind: stats.to_dict() for kind, stats in sorted(self.line_items.items())},
        }


def _excerpt(payload: str, limit: int = 140) -> str:
    flat = " ".join(payload.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _documents(docs_dir: str | Path, count: int, *, scans: bool) -> list[Path]:
    suffix = ".png" if scans else ".txt"
    paths = sorted(Path(docs_dir).glob(f"doc-*{suffix}"))
    return paths[:count] if count else paths


def household_names(docs_dir: str | Path, paths: list[Path]) -> list[str]:
    """The household's names, from the corpus ground truth.

    In production this comes from `HouseholdMemory.household_names()`. In
    the test it comes from the corpus, because the point of the test is to
    measure the router with the same information the memory layer would
    give it — a redactor told the names is the production configuration,
    so measuring it without them would understate the router.
    """
    names: set[str] = set()
    for path in paths:
        meta = path.with_suffix(".json")
        if not meta.exists():
            continue
        try:
            holder = json.loads(meta.read_text(encoding="utf-8")).get("holder")
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(holder, str) and holder.strip():
            names.add(holder.strip())
    return sorted(names)


def _score(request: OutboundRequest, names: list[str]) -> list[str]:
    """Which categories of personal detail one payload carries."""
    if request.kind == "ocr_call":
        # The payload is the document image. An image is the document, so
        # there is nothing to pattern-match: if it was sent, it leaked.
        return ["document_image"] if request.payload else []
    return [category.value for category in contains_personal_detail(request.payload, names=names)]


@contextmanager
def _capture_for(
    path: Path,
    *,
    router_on: bool,
    names: list[str],
    router: Callable[[str], str] | None = None,
):
    """Run one document through the pipeline and yield what left.

    `router` exists for the negative control: passing `lambda text: text`
    is a router that does nothing, and the harness must catch it. Without
    that control, a 0% ON rate is indistinguishable from a redaction layer
    that was never wired in.
    """
    with capture() as sink:
        result = process_document(
            path,
            today=CORPUS_TODAY,
            redact=router_on,
            transport=capturing_transport(""),
        )

        item = result.item
        if item is not None:
            # The same query in both conditions, built from the same real
            # values. The only difference is whether the router sees it.
            query = build_query(
                provider=item.provider,
                kind=item.kind,
                reference=item.reference or "",
                holder=item.holder or "",
            )
            outbound = query
            if router_on:
                if router is not None:
                    outbound = router(query)
                else:
                    outbound = redact(
                        query,
                        names=names,
                        transport=capturing_transport(NO_RESIDUAL_SPANS),
                    ).text

            TavilyClient(
                api_key="",
                transport=capturing_transport(EMPTY_SEARCH),
            ).search(outbound, redact=False)

        yield sink


def run_leak_test(
    docs_dir: str | Path = "eval/synthetic_docs",
    *,
    conditions: tuple[str, ...] = ("router_off", "router_on"),
    count: int = 0,
    scans: bool = False,
    router: Callable[[str], str] | None = None,
) -> dict[str, ConditionResult]:
    """Run the leak test.

    Args:
        docs_dir: The corpus directory.
        conditions: Which conditions to measure.
        count: How many documents to use. 0 means all of them.
        scans: Use the rendered PNGs instead of the source text.
        router: Override the redaction step. The tests pass a pass-through
            to prove a broken router is caught; production never should.

    Returns:
        A mapping of condition name to its results.
    """
    results: dict[str, ConditionResult] = {}
    paths = _documents(docs_dir, count, scans=scans)
    if not paths:
        raise FileNotFoundError(f"no documents in {docs_dir} — run: make synth")

    names = household_names(docs_dir, paths)

    for condition in conditions:
        router_on = condition == "router_on"
        result = ConditionResult(condition=condition)

        for kind in LINE_ITEM_KINDS:
            result.stats(kind)

        for path in paths:
            result.documents += 1
            with _capture_for(path, router_on=router_on, names=names, router=router) as sink:
                for request in sink:
                    stats = result.stats(request.kind)
                    stats.add(_score(request, names), payload=request.payload)

        _annotate(result)
        results[condition] = result

    return results


def _annotate(result: ConditionResult) -> None:
    """Say why a zero is a zero, so a bare 0.0% cannot be misread.

    A line item with no payloads and a line item with no leaks both render
    as 0%, and they mean opposite things. The note is what separates them.
    """
    total = result.stats("lookup_call").payloads
    ocr = result.stats("ocr_call")
    if ocr.payloads == 0:
        ocr.note = (
            "no document needed the vision model — the local extractor read every page, "
            "so no image left the machine"
        )
    else:
        ocr.note = "the document image was sent: every one of these is a leak by definition"

    redact_item = result.stats("redact_call")
    if result.condition == "router_off":
        redact_item.note = "not applicable — with no router there is no escalation call"
    elif redact_item.payloads == 0:
        redact_item.note = "no outbound payload needed an escalation"
    else:
        redact_item.note = (
            "the router's own call; carries the locally-redacted payload, not the source text"
        )

    result.stats("lookup_call").note = f"{total} queries from {result.documents} documents"


def _pct(stats: LeakStats) -> str:
    if stats.payloads == 0:
        return "—"
    return f"{stats.leak_rate * 100:.1f}%"


def render_report(results: dict[str, ConditionResult], *, docs: int, scans: bool) -> str:
    lines: list[str] = []
    lines.append("## The leak test — router ON vs OFF")
    lines.append("")
    lines.append(
        f"**Corpus:** {docs} {'rendered scans' if scans else 'documents'} · "
        f"**reference date:** {CORPUS_TODAY.isoformat()} · "
        "**network calls:** none — every payload captured at the egress seam"
    )
    lines.append("")

    lines.append("### The headline")
    lines.append("")
    lines.append("The calls that carry user data to a model or a third party.")
    lines.append("")
    lines.append("| Condition | Payloads | With personal detail | Leak rate |")
    lines.append("|---|---:|---:|---:|")
    for condition, result in results.items():
        headline = result.headline
        lines.append(
            f"| `{condition}` | {headline.payloads} | {headline.payloads_with_pii} | "
            f"{_pct(headline)} |"
        )
    lines.append("")

    off = results.get("router_off")
    on = results.get("router_on")
    if off is not None and on is not None:
        before = off.headline
        after = on.headline
        reduction = 0.0
        if before.payloads_with_pii:
            reduction = 1 - (after.payloads_with_pii / before.payloads_with_pii)
        lines.append(
            f"**{before.payloads_with_pii} → {after.payloads_with_pii} payloads carrying "
            f"personal detail ({reduction * 100:.1f}% reduction).**"
        )
        lines.append("")

    lines.append("### Line items")
    lines.append("")
    lines.append("| Condition | Line item | Payloads | With personal detail | Leak rate | Note |")
    lines.append("|---|---|---:|---:|---:|---|")
    for condition, result in results.items():
        for kind in LINE_ITEM_KINDS:
            stats = result.line_items.get(kind)
            if stats is None:
                continue
            lines.append(
                f"| `{condition}` | `{kind}` | {stats.payloads} | {stats.payloads_with_pii} | "
                f"{_pct(stats)} | {stats.note} |"
            )
    lines.append("")

    lines.append("### What this number means")
    lines.append("")
    lines.append(
        "`router_off` is the same pipeline with the privacy layer removed: the "
        "document text is not redacted, so the lookup query quotes the reference "
        "and the holder — which is what makes a search specific. `router_on` "
        "differs in exactly one place: the payload passes through "
        "`privacy_router.redact` before it is sent."
    )
    lines.append("")
    lines.append(
        "The headline counts `ocr_call` and `lookup_call`. `redact_call` is "
        "reported separately: it is the router's own outbound call, and folding "
        "it into the headline would let the router's cost flatter the router's "
        "benefit."
    )
    lines.append("")
    lines.append(
        "**The caveat that matters:** this measures the redaction layer the "
        "OpenShell middleware invokes, not the gateway applying it. The sandbox's "
        "ON condition is the presence of the `network_middlewares` entry, and that "
        "leg needs the gateway host. Until it runs, the honest claim is \"the "
        "router's coverage is X\", not \"the sandbox enforced X\"."
    )
    lines.append("")
    lines.append(
        "No request was sent. Every payload was captured in-process, so this "
        "costs nothing and is identical on every run."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", default="eval/synthetic_docs")
    parser.add_argument("--count", type=int, default=0, help="documents to use (0 = all)")
    parser.add_argument("--scans", action="store_true", help="use the rendered PNGs")
    parser.add_argument("--json", dest="json_out", default="eval/results/leak_test.json")
    parser.add_argument("--strict", action="store_true", help="exit 1 if the router ON leg leaks")
    args = parser.parse_args(argv)

    try:
        results = run_leak_test(args.docs, count=args.count, scans=args.scans)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    docs = next(iter(results.values())).documents if results else 0
    print(render_report(results, docs=docs, scans=args.scans))

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "corpus": str(args.docs),
            "documents": docs,
            "scans": args.scans,
            "reference_date": CORPUS_TODAY.isoformat(),
            "conditions": {name: result.to_dict() for name, result in results.items()},
        }
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")

    if args.strict:
        on = results.get("router_on")
        if on is not None and on.headline.payloads_with_pii:
            print("\nstrict: the router ON leg leaked", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
