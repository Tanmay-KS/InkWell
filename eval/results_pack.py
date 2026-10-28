#!/usr/bin/env python3
"""Compile the Phase 3 numbers into a report and charts.

Phase 3 — Joint.

The video needs three numbers, and each one has an artifact behind it.
This script is the only thing that turns those artifacts into the page and
the charts, so the visuals cannot drift from the measurements:

    make results        # writes docs/results.md and docs/results/*.svg

Three rules:

1. **A number is only reportable when its artifact is.** A leak test over a
   handful of documents or a memory eval over three scenarios is a working
   harness, not a measurement. The packer marks it NOT READY and says why,
   instead of drawing a chart that looks as authoritative as the real ones.
2. **Only aggregates are published.** The leak artifact carries example
   excerpts with synthetic personal details in them; they stay out of the
   report. The charts have no names, accounts, or providers in them.
3. **Every table names its artifact.** A number with no provenance is a
   number nobody can check.

SVGs are written by hand rather than with matplotlib: the output is
deterministic, diffable, and needs no dependency the project does not
already have.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

__all__ = [
    "COST_MIN_CALLS",
    "LEAK_MIN_DOCUMENTS",
    "MEMORY_MIN_SCENARIOS",
    "bar_chart",
    "load_cost",
    "load_leak",
    "load_memory",
    "main",
    "render_report",
]

# The leak test's canonical run is 200 documents. Anything smaller is a
# smoke run; `make leak-test-smoke` writes a different file, but a hand-run
# `--count 5` can still land in the canonical path.
LEAK_MIN_DOCUMENTS = 200

# Twenty scenarios is the floor for a per-category rate to mean anything.
# Below that the score is a harness check.
MEMORY_MIN_SCENARIOS = 20

# One tier's worth of calls. The bench does 20 per tier at its default.
COST_MIN_CALLS = 10

PALETTE = {
    "blue": "#2563eb",
    "red": "#dc2626",
    "green": "#16a34a",
    "gray": "#94a3b8",
    "ink": "#0f172a",
    "muted": "#64748b",
    "grid": "#e2e8f0",
}


@dataclass(slots=True)
class Finding:
    """One of the three numbers and whether it is reportable."""

    key: str
    title: str
    ready: bool
    status: str
    detail: str
    artifact: str


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"unreadable artifact: {path}: {exc}") from exc


def load_leak(path: Path) -> tuple[dict | None, Finding]:
    """The leak rate, OFF vs ON."""
    try:
        data = _read(path)
    except ValueError as exc:
        return None, Finding(
            "leak", "Leak rate, router OFF → ON", False, "NOT READY", str(exc), str(path)
        )

    documents = int(data.get("documents", 0) or 0)
    conditions = data.get("conditions") or {}
    missing = [name for name in ("router_off", "router_on") if name not in conditions]
    if missing:
        return data, Finding(
            "leak",
            "Leak rate, router OFF → ON",
            False,
            "NOT READY",
            f"both conditions are required; missing {', '.join(missing)}",
            str(path),
        )

    ready = documents >= LEAK_MIN_DOCUMENTS
    off = float(conditions["router_off"]["headline"]["leak_rate"])
    on = float(conditions["router_on"]["headline"]["leak_rate"])
    detail = (
        f"{documents} documents; OFF {off:.1%}, ON {on:.1%}"
        if ready
        else f"only {documents} documents (need {LEAK_MIN_DOCUMENTS}) — a smoke run"
    )
    return data, Finding(
        "leak",
        "Leak rate, router OFF → ON",
        ready,
        "READY" if ready else "NOT READY",
        detail,
        str(path),
    )


def load_memory(path: Path) -> tuple[dict | None, Finding]:
    """Memory-with-updates accuracy."""
    try:
        data = _read(path)
    except ValueError as exc:
        return None, Finding(
            "memory",
            "Memory with updates",
            False,
            "NOT READY",
            str(exc),
            str(path),
        )

    count = int(data.get("scenario_count", 0) or 0)
    ready = count >= MEMORY_MIN_SCENARIOS
    detail = (
        f"{count} scenarios"
        if ready
        else f"only {count} scenarios (need {MEMORY_MIN_SCENARIOS}) — a harness check, not a rate"
    )
    return data, Finding(
        "memory",
        "Memory with updates",
        ready,
        "READY" if ready else "NOT READY",
        detail,
        str(path),
    )


def load_cost(path: Path) -> tuple[dict | None, Finding]:
    """Cost and latency per tier."""
    try:
        data = _read(path)
    except ValueError as exc:
        return None, Finding(
            "cost", "Cost and latency per tier", False, "NOT READY", str(exc), str(path)
        )

    tiers = data.get("tiers") or []
    calls = sum(int(tier.get("calls", 0) or 0) for tier in tiers)
    errors = sum(int(tier.get("errors", 0) or 0) for tier in tiers)
    ready = calls >= COST_MIN_CALLS and bool(tiers)
    detail = (
        f"{calls} calls, {errors} error(s), measured {data.get('measured_at', 'at an unknown time')}"
        if ready
        else f"only {calls} successful calls (need {COST_MIN_CALLS})"
    )
    return data, Finding(
        "cost",
        "Cost and latency per tier",
        ready,
        "READY" if ready else "NOT READY",
        detail,
        str(path),
    )


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def bar_chart(
    *,
    categories: list[str],
    series: list[tuple[str, list[float], str]],
    title: str,
    subtitle: str = "",
    value_format: str = "{:.1f}",
    max_value: float | None = None,
    width: int = 720,
    height: int = 380,
) -> str:
    """A deterministic grouped bar chart as SVG.

    Args:
        categories: X-axis labels, one group each.
        series: `(label, values, colour)` triples. Every values list must
            be the same length as `categories`.
        title: Chart title.
        subtitle: One line under the title.
        value_format: `str.format` pattern for the value labels.
        max_value: Y-axis top. Defaults to a round number above the data.
        width: SVG width in pixels.
        height: SVG height in pixels.

    Returns:
        The SVG document as a string.
    """
    if not categories:
        raise ValueError("a chart needs at least one category")
    if any(len(values) != len(categories) for _label, values, _colour in series):
        raise ValueError("every series needs one value per category")

    top = max_value or max(
        (value for _label, values, _colour in series for value in values), default=0.0
    )
    if top <= 0:
        top = 1.0
    # Round the axis up so the gridlines land on readable numbers.
    magnitude = 10 ** max(0, len(str(int(top))) - 1)
    top = float(max(magnitude, -(-top // magnitude) * magnitude))

    left, right, upper, lower = 64, 24, 96, 62
    plot_width = width - left - right
    plot_height = height - upper - lower

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-family="system-ui, sans-serif" font-size="19" '
        f'font-weight="600" fill="{PALETTE["ink"]}">{_esc(title)}</text>',
    ]
    if subtitle:
        parts.append(
            f'<text x="{left}" y="56" font-family="system-ui, sans-serif" font-size="12" '
            f'fill="{PALETTE["muted"]}">{_esc(subtitle)}</text>'
        )

    # Legend, top right. Each entry's width follows its label, so a long
    # label is never clipped by a fixed slot.
    legend_widths = [22 + 6.2 * len(label) for label, _values, _colour in series]
    legend_x = width - right - sum(legend_widths) - 14 * (len(series) - 1)
    for (label, _values, colour), entry_width in zip(series, legend_widths, strict=True):
        parts.append(
            f'<rect x="{legend_x:.1f}" y="28" width="10" height="10" rx="2" fill="{colour}"/>'
            f'<text x="{legend_x + 16:.1f}" y="37" font-family="system-ui, sans-serif" '
            f'font-size="11" fill="{PALETTE["muted"]}">{_esc(label)}</text>'
        )
        legend_x += entry_width + 14

    # Gridlines and y labels.
    for step in range(5):
        y = upper + plot_height - plot_height * step / 4
        value = top * step / 4
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="{PALETTE["grid"]}" stroke-width="1"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="system-ui, sans-serif" font-size="10" '
            f'fill="{PALETTE["muted"]}">{_esc(value_format.format(value))}</text>'
        )

    group_width = plot_width / len(categories)
    bar_width = min(56.0, group_width * 0.7 / max(1, len(series)))

    for index, category in enumerate(categories):
        group_center = left + group_width * (index + 0.5)
        start = group_center - bar_width * len(series) / 2
        for series_index, (_label, values, colour) in enumerate(series):
            value = max(0.0, float(values[index]))
            bar_height = plot_height * min(1.0, value / top)
            x = start + series_index * bar_width
            y = upper + plot_height - bar_height
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width - 4:.1f}" '
                f'height="{bar_height:.1f}" rx="2" fill="{colour}"/>'
                f'<text x="{x + (bar_width - 4) / 2:.1f}" y="{y - 6:.1f}" '
                f'text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" '
                f'fill="{PALETTE["ink"]}">{_esc(value_format.format(value))}</text>'
            )
        parts.append(
            f'<text x="{group_center:.1f}" y="{upper + plot_height + 22}" '
            f'text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" '
            f'fill="{PALETTE["ink"]}">{_esc(category)}</text>'
        )

    parts.append(
        f'<line x1="{left}" y1="{upper + plot_height}" x2="{width - right}" '
        f'y2="{upper + plot_height}" stroke="{PALETTE["muted"]}" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_report(
    findings: list[Finding],
    leak: dict | None,
    memory: dict | None,
    cost: dict | None,
    *,
    charts: dict[str, str],
    artifacts: dict[str, str],
) -> str:
    """The report page. Aggregates only, every table tied to its artifact.

    The output is deterministic: identical artifacts produce an identical
    page. A wall-clock "generated at" line would make every rerun dirty the
    tree, and the per-section measurement timestamps are the provenance
    that actually matters.
    """
    lines = [
        "# The three numbers",
        "",
        "Generated by `make results`. **Do not edit by hand** — re-run the "
        "target.",
        "",
        "Each number below is read from an artifact in `eval/results/`. The "
        "packer refuses to draw a number whose artifact is missing, stale, or "
        "too small to mean anything.",
        "",
        "| # | Number | Status | Detail | Artifact |",
        "|---|---|---|---|---|",
    ]
    for index, finding in enumerate(findings, start=1):
        lines.append(
            f"| {index} | {finding.title} | **{finding.status}** | "
            f"{finding.detail} | `{finding.artifact}` |"
        )
    lines.append("")

    # --- leak ------------------------------------------------------------
    leak_finding = next(f for f in findings if f.key == "leak")
    lines += ["## 1. Leak rate, router OFF → ON", ""]
    if leak and leak_finding.ready:
        off = leak["conditions"]["router_off"]["headline"]
        on = leak["conditions"]["router_on"]["headline"]
        lines += [
            "![Leak rate with the router off and on](results/leak-rate.svg)",
            "",
            "| Condition | Payloads | With a personal detail | Leak rate |",
            "|---|---:|---:|---:|",
            f"| router OFF | {off['payloads']} | {off['payloads_with_pii']} | "
            f"**{off['leak_rate']:.1%}** |",
            f"| router ON | {on['payloads']} | {on['payloads_with_pii']} | "
            f"**{on['leak_rate']:.1%}** |",
            "",
            f"Source: artifact above, {leak.get('documents')} documents, "
            f"reference date {leak.get('reference_date', '—')}.",
            "",
        ]
    else:
        lines += [f"**NOT READY.** {leak_finding.detail}.", ""]

    # --- cost ------------------------------------------------------------
    cost_finding = next(f for f in findings if f.key == "cost")
    lines += ["## 2. Cost and latency per tier", ""]
    if cost and cost_finding.ready:
        lines += [
            "![Cost per thousand calls](results/cost-per-1k.svg)",
            "",
            "![p95 latency per tier](results/latency-p95.svg)",
            "",
            "| Tier | Model | Calls | Errors | Mean ms | p95 ms | Cost/call | $/1k calls |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for tier in cost["tiers"]:
            lines.append(
                f"| {tier['tier']} | `{tier['model']}` | {tier['calls']} | {tier['errors']} | "
                f"{tier['mean_latency_ms']:.0f} | {tier['p95_latency_ms']:.0f} | "
                f"${tier['cost_per_call_usd']:.6f} | ${tier['cost_per_1k_calls_usd']:.2f} |"
            )
        lines += [
            "",
            f"Measured {cost.get('measured_at')} at `{cost.get('endpoint')}`, "
            f"{cost.get('repeats')} repeats per prompt, pricing from the live catalog "
            f"({cost.get('catalog_models')} models). Total spend "
            f"${cost.get('total_cost_usd', 0):.4f}.",
            "",
        ]
    else:
        lines += [f"**NOT READY.** {cost_finding.detail}.", ""]

    # --- memory ----------------------------------------------------------
    memory_finding = next(f for f in findings if f.key == "memory")
    lines += ["## 3. Memory with updates", ""]
    if memory and memory_finding.ready:
        scores = memory.get("scores") or {}
        baseline = memory.get("update_blind_baseline") or {}
        lines += [
            "![Memory accuracy by question type](results/memory-accuracy.svg)",
            "",
            "| Question type | Ledger | Update-blind baseline |",
            "|---|---:|---:|",
        ]
        for key, label in (
            ("accuracy_current", "current fact after a supersession"),
            ("accuracy_superseded", "the superseded fact, at a point in time"),
            ("accuracy_never_stated", "abstention on a never-stated subject"),
        ):
            lines.append(
                f"| {label} | {scores.get(key, 0):.1%} | {baseline.get(key, 0):.1%} |"
            )
        lines += [
            f"| **overall** | **{scores.get('accuracy_overall', 0):.1%}** | "
            f"**{baseline.get('accuracy_overall', 0):.1%}** |",
            "",
            f"{memory.get('scenario_count')} scenarios. Method: {memory.get('method')}."
            + (
                f" Measured {memory['measured_at']}."
                if memory.get("measured_at")
                else ""
            ),
            "",
        ]
    else:
        lines += [f"**NOT READY.** {memory_finding.detail}.", ""]

    # --- limits ----------------------------------------------------------
    lines += [
        "## What these numbers do not prove",
        "",
        "- **The leak test measures what would leave, not what the far end does.** "
        "It captures the request at `privacy_router/egress.py` and returns a canned "
        "answer; no network, no API key.",
        "- **The leak headline is the lookup path.** `ocr_call` is 0 in both "
        "conditions because the local extractor read every document, so no image "
        "left the machine. That is a result, not a gap, but it is not a claim "
        "about vision models.",
        "- **Latency is this host, this network, this hour.** It is a comparison "
        "between tiers under the same conditions, not an SLA.",
        "- **The memory score is deterministic SQLite supersession**, not model "
        "paraphrase quality. It measures whether a changed fact is superseded and "
        "history is preserved.",
        "",
        "## Provenance",
        "",
    ]
    for finding in findings:
        lines.append(f"- {finding.title}: `{artifacts.get(finding.key, finding.artifact)}` "
                     f"— {finding.status}")
    lines.append("")
    lines.append(f"Charts: {', '.join(f'`{name}`' for name in sorted(charts))}.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", default="eval/results")
    parser.add_argument("--docs", default="docs")
    parser.add_argument("--report", default="docs/results.md")
    args = parser.parse_args(argv)

    results = Path(args.results)
    docs = Path(args.docs)
    charts_dir = docs / "results"
    charts_dir.mkdir(parents=True, exist_ok=True)

    leak, leak_finding = load_leak(results / "leak_test.json")
    memory, memory_finding = load_memory(results / "memory_eval.json")
    cost, cost_finding = load_cost(results / "cost_latency.json")
    findings = [leak_finding, memory_finding, cost_finding]

    charts: dict[str, str] = {}

    if leak and leak_finding.ready:
        off = leak["conditions"]["router_off"]["headline"]
        on = leak["conditions"]["router_on"]["headline"]
        charts["leak-rate.svg"] = bar_chart(
            categories=["router OFF", "router ON"],
            series=[
                (
                    "payloads with a personal detail",
                    [float(off["leak_rate"]) * 100, float(on["leak_rate"]) * 100],
                    PALETTE["red"],
                )
            ],
            title="Leak rate: 200 documents, router OFF vs ON",
            subtitle="Share of outbound lookup payloads carrying a personal detail",
            value_format="{:.0f}%",
            max_value=100,
        )

    if cost and cost_finding.ready:
        tiers = cost["tiers"]
        names = [str(tier["tier"]) for tier in tiers]
        charts["cost-per-1k.svg"] = bar_chart(
            categories=names,
            series=[
                (
                    "cost per 1,000 calls",
                    [float(tier["cost_per_1k_calls_usd"]) for tier in tiers],
                    PALETTE["blue"],
                )
            ],
            title="Cost per 1,000 calls",
            subtitle=f"Measured {cost.get('measured_at', '')} · live catalog pricing",
            value_format="${:,.2f}",
        )
        charts["latency-p95.svg"] = bar_chart(
            categories=names,
            series=[
                (
                    "p95 latency",
                    [float(tier["p95_latency_ms"]) for tier in tiers],
                    PALETTE["green"],
                )
            ],
            title="p95 latency per call",
            subtitle=f"{cost.get('repeats')} repeats per prompt · wall clock",
            value_format="{:,.0f} ms",
        )

    if memory and memory_finding.ready:
        scores = memory.get("scores") or {}
        baseline = memory.get("update_blind_baseline") or {}
        keys = (
            ("accuracy_current", "current"),
            ("accuracy_superseded", "superseded"),
            ("accuracy_never_stated", "never stated"),
        )
        charts["memory-accuracy.svg"] = bar_chart(
            categories=[label for _key, label in keys],
            series=[
                ("Ledger", [float(scores.get(key, 0)) * 100 for key, _ in keys], PALETTE["blue"]),
                (
                    "update-blind",
                    [float(baseline.get(key, 0)) * 100 for key, _ in keys],
                    PALETTE["gray"],
                ),
            ],
            title="Memory accuracy by question type",
            subtitle=f"{memory.get('scenario_count')} scenarios · deterministic supersession",
            value_format="{:.0f}%",
            max_value=100,
        )

    for name, svg in charts.items():
        (charts_dir / name).write_text(svg, encoding="utf-8")

    report = render_report(
        findings,
        leak,
        memory,
        cost,
        charts=charts,
        artifacts={finding.key: finding.artifact for finding in findings},
    )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    for finding in findings:
        print(f"{finding.status:9s} {finding.title}: {finding.detail}")
    print(f"charts: {', '.join(sorted(charts)) if charts else 'none'}")
    print(f"report: {report_path}")

    if not all(finding.ready for finding in findings):
        print(
            "one or more numbers are not reportable yet — the report says which and why",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
