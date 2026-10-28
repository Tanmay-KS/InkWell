"""Background jobs.

Phase 3 — Person A.

`deadline_monitor.py` is the Nebius Serverless Jobs entrypoint for
background deadline monitoring.

The re-export is lazy on purpose: `python -m jobs.deadline_monitor` is the
way the job container starts, and importing the module from the package
`__init__` first makes the interpreter warn about running a module that is
already in `sys.modules`.
"""

from __future__ import annotations

__all__ = ["JobConfig", "run_once"]


def __getattr__(name: str) -> object:
    if name in __all__:
        from jobs import deadline_monitor

        return getattr(deadline_monitor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
