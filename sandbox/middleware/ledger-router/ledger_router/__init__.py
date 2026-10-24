"""Ledger privacy router — OpenShell supervisor middleware.

The service is deliberately small: the decision logic lives in
`privacy_router` (shared with the in-process pipeline and the leak test),
and this package is the transport that puts it in front of every request
the sandbox is allowed to make.

The generated protobuf modules are kept in `generated/` and put on
`sys.path` here because the vendored `supervisor_middleware.proto` imports
`extension.proto` as a top-level module (`import extension_pb2`), which is
how OpenShell ships it. Re-generating the stubs does not require a change
to this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_MIDDLEWARE_DIR = _PACKAGE_DIR.parent
_REPO_ROOT = _MIDDLEWARE_DIR.parents[2]
_GENERATED = _MIDDLEWARE_DIR / "generated"

for _path in (_MIDDLEWARE_DIR, _GENERATED, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

__all__ = ["__version__"]

__version__ = "0.1.0"
