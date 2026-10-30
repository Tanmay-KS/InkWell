"""InkWell agent pipeline.

Document in → deadline flagged → draft action out.

The pipeline is deliberately a set of small, separately testable stages rather
than one agent loop, because every stage boundary is also a place where
personal details can be inspected, redacted, and logged.

Stages:

    ingestion/   bytes → structured text
    memory/      structured text + household facts → what we already know
    deadlines/   structured text → dated obligations
    actions/     dated obligations → a draft the user can send

Ownership: Person B. See IMPLEMENTATION.md for the phase plan.
"""

__all__ = ["actions", "deadlines", "ingestion", "memory"]
