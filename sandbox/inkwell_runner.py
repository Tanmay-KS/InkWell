#!/usr/bin/env python3
"""Launch InkWell's demo without colliding with Hermes's own `agent` package.

OpenShell strips `PYTHONPATH` from the sandbox environment, so the image's
`ENV PYTHONPATH=/opt/inkwell` is not enough on its own: the interpreter has to
be told where InkWell's packages live. This launcher is that instruction.

The upstream Hermes image already ships an importable `agent` package, so the
failure mode to guard against is silent: Python finds Hermes's `agent`, and
the pipeline fails later with an error that has nothing to do with the cause.
The assertions below make that collision immediate and specific.

    /opt/hermes/.venv/bin/python /opt/inkwell/inkwell_runner.py <document> [...]
    /opt/hermes/.venv/bin/python /opt/inkwell/inkwell_runner.py --where
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def inkwell_root(here: Path) -> Path:
    """Return the directory holding InkWell's packages.

    Installed in the image, this file is /opt/inkwell/inkwell_runner.py and the
    packages are its siblings. Run from a checkout, it is
    sandbox/inkwell_runner.py and the packages are one level up. Discover the
    layout rather than assuming a nesting depth: assuming one is exactly the
    mistake that made the image import Hermes's `agent` instead of InkWell's.
    """
    for candidate in (here, here.parent):
        if (candidate / "agent" / "__init__.py").is_file() and (
            candidate / "privacy_router" / "__init__.py"
        ).is_file():
            return candidate
    raise SystemExit(
        f"inkwell_runner: no InkWell packages next to or above {here}. "
        "Expected agent/ and privacy_router/ under /opt/inkwell."
    )


INKWELL_ROOT = inkwell_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(INKWELL_ROOT))

# Deliberately after the sys.path edit: this is the import the collision
# guard is about.
import agent

_resolved = Path(agent.__file__).resolve().parent
if _resolved != INKWELL_ROOT / "agent":
    raise SystemExit(
        f"inkwell_runner: 'agent' resolved to {_resolved}, not "
        f"{INKWELL_ROOT / 'agent'}. Hermes's own 'agent' package is "
        "shadowing InkWell's — check that the image copied the packages to "
        "/opt/inkwell."
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
