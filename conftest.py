"""Pytest configuration.

The repository root has to be importable so that the tests can reach the
project's own packages — `eval.scoring`, `privacy_router.redact` — the
same way the scripts do. Adding it here means every test module can just
import them, with no `sys.path` juggling in each file.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
