#!/usr/bin/env python3
"""Enable or disable the router registration in an OpenShell gateway TOML.

The committed `sandbox/gateway.toml` keeps the middleware block commented
out. That is deliberate: the gateway calls every registered middleware at
startup and refuses to start if one is unavailable. A repository clone must
not take the gateway down just because the Phase 2 service is not running.

This helper toggles only that block, validates the result with `tomllib`, and
prints or writes the file. The shell wrappers (`router_register.sh`,
`router_unregister.sh`) own the backup, preflight, install and restart.

Usage:
    python3 sandbox/gateway_config.py --enable  --output /tmp/gateway.toml
    python3 sandbox/gateway_config.py --disable --output /tmp/gateway.toml
    python3 sandbox/gateway_config.py --check --input /tmp/gateway.toml
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

BLOCK_MARKER = "[[openshell.supervisor.middleware]]"
COMMENTED_MARKER = f"# {BLOCK_MARKER}"


def enable(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    in_block = False
    found = False
    for line in lines:
        if line.strip() == COMMENTED_MARKER:
            found = True
            in_block = True
            output.append(BLOCK_MARKER)
            continue
        if in_block:
            if not line.strip() or line.strip() == "#":
                # The block is separated from the prose below it by a
                # blank line or a lone `#` comment line.
                in_block = False
                output.append("")
            elif line.startswith("# "):
                output.append(line[2:])
            elif line.startswith("#"):
                output.append(line[1:])
            else:
                # The block was already uncommented, or the file has a
                # shape this helper does not understand. Stop touching it.
                in_block = False
                output.append(line)
            continue
        output.append(line)
    if not found and not any(line.strip() == BLOCK_MARKER for line in lines):
        raise SystemExit(f"{COMMENTED_MARKER!r} not found")
    return "\n".join(output) + "\n"


def disable(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    in_block = False
    for line in lines:
        if line.strip() == BLOCK_MARKER:
            in_block = True
            output.append(COMMENTED_MARKER)
            continue
        if in_block:
            if not line.strip():
                in_block = False
                output.append(line)
            elif line.lstrip().startswith("#"):
                output.append(line)
            else:
                output.append(f"# {line}")
            continue
        output.append(line)
    return "\n".join(output) + "\n"


def validate(text: str) -> None:
    data = tomllib.loads(text)
    openshell = data.get("openshell")
    if not isinstance(openshell, dict):
        raise SystemExit("missing [openshell] table")
    supervisor = openshell.get("supervisor")
    middlewares = supervisor.get("middleware") if isinstance(supervisor, dict) else None
    if middlewares is None:
        return
    if not isinstance(middlewares, list):
        raise SystemExit("openshell.supervisor.middleware is not an array of tables")
    for entry in middlewares:
        if not isinstance(entry, dict):
            raise SystemExit("middleware entry is not a table")
        if "name" not in entry or "grpc_endpoint" not in entry:
            raise SystemExit("middleware entry needs name and grpc_endpoint")


def middleware_names(text: str) -> list[str]:
    data = tomllib.loads(text)
    entries = (
        data.get("openshell", {})
        .get("supervisor", {})
        .get("middleware", [])
    )
    return [str(entry.get("name", "")) for entry in entries if isinstance(entry, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--input", default="sandbox/gateway.toml")
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)

    text = Path(args.input).read_text(encoding="utf-8")
    if args.check:
        validate(text)
        names = middleware_names(text)
        print("valid gateway TOML" + (f"; middleware: {', '.join(names)}" if names else ""))
        return 0

    result = enable(text) if args.enable else disable(text)
    validate(result)
    if args.output == "-":
        sys.stdout.write(result)
    else:
        Path(args.output).write_text(result, encoding="utf-8")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
