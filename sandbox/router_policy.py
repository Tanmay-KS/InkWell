#!/usr/bin/env python3
"""Generate the router-enabled OpenShell policy from the base policy.

The base policy (`sandbox/policy.yaml`) deliberately has the
`network_middlewares` block commented out. That file has to stay applyable
on a host where the router service is not running, because the gateway
refuses a policy that names an unavailable middleware.

This script produces the Phase 2 variant without copying the file: it loads
the base, adds the entry, validates the router config against the same
Pydantic schema the service uses, and writes YAML. There is one source of
truth for the endpoints and filesystem rules, and no second policy file to
drift.

Usage:
    python3 sandbox/router_policy.py --on  --output /tmp/policy.yaml
    python3 sandbox/router_policy.py --off --output /tmp/policy.yaml
    python3 sandbox/router_policy.py --check

`--off` reproduces the base policy. Applying either file is a separate,
deliberate step (`sandbox/router_on.sh` / `sandbox/router_off.sh`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
MIDDLEWARE_DIR = REPO_ROOT / "sandbox" / "middleware" / "ledger-router"

if str(MIDDLEWARE_DIR) not in sys.path:
    sys.path.insert(0, str(MIDDLEWARE_DIR))

from ledger_router.config import RouterConfig

BASE_POLICY = SCRIPT_DIR / "policy.yaml"

MIDDLEWARE_NAME = "ledger-router"
MIDDLEWARE_CONFIG_NAME = "ledger_router"

DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "redact",
    "redact_models": [
        "nvidia/Nemotron-3.5-Super-VL",
        "nvidia/Nemotron-3-Ultra-550b-a55b",
    ],
    "redact_hosts": ["api.tavily.com"],
    "allowed_hosts": [
        "api.tokenfactory.nebius.com",
        "api.tavily.com",
    ],
    "third_party_hosts": ["api.tavily.com"],
    "ocr_models": ["nvidia/Nemotron-3.5-Super-VL"],
    "forbidden_flags": ["include_answer", "include_raw_content"],
    "deny_threshold": 3,
    "escalate": False,
    "audit_log_path": "/var/log/ledger/audit.jsonl",
}

ENDPOINT_HOSTS = [
    "api.tokenfactory.nebius.com",
    "api.tavily.com",
]


def build_router_entry(audit_log: str, *, escalate: bool = False) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    config["audit_log_path"] = audit_log
    config["escalate"] = escalate
    RouterConfig(**config)  # fail here, not on the gateway
    return {
        "name": "Ledger privacy router",
        "middleware": MIDDLEWARE_NAME,
        "order": 10,
        "on_error": "fail_closed",
        "config": config,
        "endpoints": {"include": list(ENDPOINT_HOSTS)},
    }


def build_policy(
    base: dict[str, Any],
    *,
    enabled: bool,
    audit_log: str,
    escalate: bool = False,
) -> dict[str, Any]:
    policy = dict(base)
    if not enabled:
        policy.pop("network_middlewares", None)
        return policy
    policy["network_middlewares"] = {
        MIDDLEWARE_CONFIG_NAME: build_router_entry(audit_log, escalate=escalate)
    }
    return policy


def load_base() -> dict[str, Any]:
    data = yaml.safe_load(BASE_POLICY.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{BASE_POLICY} did not parse as a mapping")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--on", action="store_true", help="add the middleware entry (default)")
    mode.add_argument("--off", action="store_true", help="produce the base policy without it")
    mode.add_argument("--check", action="store_true", help="validate the config and exit")
    parser.add_argument("--output", default="-", help="output path, or - for stdout")
    parser.add_argument(
        "--audit-log",
        default=DEFAULT_CONFIG["audit_log_path"],
        help="host path for the router audit JSONL",
    )
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="enable the Nano escalation (needs NEBIUS_API_KEY on the service host)",
    )
    args = parser.parse_args(argv)

    enabled = not args.off
    if args.check:
        RouterConfig(**{**DEFAULT_CONFIG, "audit_log_path": args.audit_log})
        print("ledger-router config valid")
        return 0

    policy = build_policy(
        load_base(),
        enabled=enabled,
        audit_log=args.audit_log,
        escalate=args.escalate,
    )
    text = yaml.safe_dump(policy, sort_keys=False, default_flow_style=False)
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
