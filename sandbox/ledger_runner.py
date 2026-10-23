#!/usr/bin/env python3
"""Launch Ledger's demo without colliding with Hermes's own `agent` package.

OpenShell strips `PYTHONPATH` from the sandbox environment, so the image's
`ENV PYTHONPATH=/opt/ledger` is not enough on its own: the interpreter has to
be told where Ledger's packages live. This launcher is that instruction.

The upstream Hermes image already ships an importable `agent` package, so the
failure mode to guard against is silent: Python finds Hermes's `agent`, and
the pipeline fails later with an error that has nothing to do with the cause.
The assertions below make that collision immediate and specific.

    /opt/hermes/.venv/bin/python /opt/ledger/ledger_runner.py <document> [...]
    /opt/hermes/.venv/bin/python /opt/ledger/ledger_runner.py --where
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def ledger_root(here: Path) -> Path:
    """Return the directory holding Ledger's packages.

    Installed in the image, this file is /opt/ledger/ledger_runner.py and the
    packages are its siblings. Run from a checkout, it is
    sandbox/ledger_runner.py and the packages are one level up. Discover the
    layout rather than assuming a nesting depth: assuming one is exactly the
    mistake that made the image import Hermes's `agent` instead of Ledger's.
    """
    for candidate in (here, here.parent):
        if (candidate / "agent" / "__init__.py").is_file() and (
            candidate / "privacy_router" / "__init__.py"
        ).is_file():
            return candidate
    raise SystemExit(
        f"ledger_runner: no Ledger packages next to or above {here}. "
        "Expected agent/ and privacy_router/ under /opt/ledger."
    )


LEDGER_ROOT = ledger_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(LEDGER_ROOT))

# Deliberately after the sys.path edit: this is the import the collision
# guard is about.
import agent

_resolved = Path(agent.__file__).resolve().parent
if _resolved != LEDGER_ROOT / "agent":
    raise SystemExit(
        f"ledger_runner: 'agent' resolved to {_resolved}, not "
        f"{LEDGER_ROOT / 'agent'}. Hermes's own 'agent' package is "
        "shadowing Ledger's — check that the image copied the packages to "
        "/opt/ledger."
    )

if __name__ == "__main__":
    # A tiny probe so --check can prove *which* package was imported without
    # running the pipeline.
    if "--where" in sys.argv[1:]:
        print(agent.__file__)
        raise SystemExit(0)

    # run_module leaves sys.argv intact, so the pipeline's arguments are
    # forwarded verbatim, and SystemExit from agent.demo becomes our exit
    # status.
    runpy.run_module("agent.demo", run_name="__main__")
