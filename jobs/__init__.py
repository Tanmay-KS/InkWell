"""Background jobs package.

Phase 3 — Person A.
`deadline_monitor.py` is the Nebius Serverless Jobs entrypoint for
background deadline monitoring.

The exports are lazy on purpose. `python -m jobs.deadline_monitor` imports
the package before it runs the module, so an eager re-export here would put
`jobs.deadline_monitor` in `sys.modules` twice and provoke a runpy warning.
"""

from __future__ import annotations

from typing import Any

__all__ = ["JobConfig", "main", "run_once", "seed_due_item"]


def __getattr__(name: str) -> Any:
    if name in {"JobConfig", "main", "run_once"}:
        from jobs import deadline_monitor

        return getattr(deadline_monitor, name)
    if name == "seed_due_item":
        from jobs import seed_due_item

        return seed_due_item.seed_due_item
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
