"""The Phase 2 operator scripts are part of the deliverable.

They cannot be run in CI without a gateway, but a syntax error or a script
that drives the wrong layer would make the live procedure unfalsifiable.
These tests check the syntax and the few structural properties that matter:
egress is probed from inside the sandbox, registration probes the service
before touching the gateway config, and the verifier actually runs the demo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "sandbox"
DEMO = REPO_ROOT / "demo" / "injection"

SHELL_SCRIPTS = [
    SANDBOX / "egress_probe.sh",
    SANDBOX / "router_on.sh",
    SANDBOX / "router_off.sh",
    SANDBOX / "router_register.sh",
    SANDBOX / "router_unregister.sh",
    SANDBOX / "verify_phase2.sh",
    SANDBOX / "select_python.sh",
    DEMO / "run_injection_demo.sh",
]


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_script_is_valid_bash(script: Path):
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_egress_probe_runs_inside_the_sandbox_not_from_the_host():
    text = (SANDBOX / "egress_probe.sh").read_text(encoding="utf-8")
    assert "openshell sandbox exec" in text
    assert "/usr/bin/curl" in text


def test_egress_probe_covers_host_path_and_port_boundaries():
    text = (SANDBOX / "egress_probe.sh").read_text(encoding="utf-8")
    assert "https://example.com/" in text
    assert "/not-a-policy-path" in text
    assert ":8443" in text
    assert "api.tavily.com/search" in text


def test_egress_probe_does_not_interpolate_a_real_credential():
    text = (SANDBOX / "egress_probe.sh").read_text(encoding="utf-8")
    assert "NEBIUS_API_KEY" not in text
    assert "Authorization:" not in text


def test_router_register_probes_before_it_installs_anything():
    text = (SANDBOX / "router_register.sh").read_text(encoding="utf-8")
    probe = text.index("ledger-router/probe.py")
    preflight = text.index("openshell-gateway config preflight --path")
    install = text.index('install -m 600 "$TMP_CONFIG" "$CONFIG"')
    assert probe < preflight < install


def test_router_register_has_a_failure_rollback():
    text = (SANDBOX / "router_register.sh").read_text(encoding="utf-8")
    assert "BACKUP=" in text
    assert "restoring the previous config" in text
    assert "install -m 600 \"$BACKUP\"" in text


def test_router_on_restores_the_base_policy_if_the_router_policy_is_refused():
    text = (SANDBOX / "router_on.sh").read_text(encoding="utf-8")
    assert "policy set \"$SANDBOX_NAME\" --policy \"${SCRIPT_DIR}/policy.yaml\"" in text
    assert "re-applying the base policy" in text


def test_router_off_applies_the_base_policy():
    text = (SANDBOX / "router_off.sh").read_text(encoding="utf-8")
    assert "${SCRIPT_DIR}/policy.yaml" in text
    assert "network_middlewares" in text or "ledger_router" in text


def test_router_unregister_refuses_while_the_policy_is_attached():
    text = (SANDBOX / "router_unregister.sh").read_text(encoding="utf-8")
    assert "router_off.sh" in text
    assert "--force" in text
    # A failed policy read is not a clean policy.
    assert "cannot read the policy" in text


def test_phase2_verifier_runs_the_injection_demo_by_default():
    text = (SANDBOX / "verify_phase2.sh").read_text(encoding="utf-8")
    assert "run_injection_demo.sh" in text
    assert "RUN_DEMO=1" in text
    assert "--no-demo" in text


def test_phase2_verifier_checks_the_audit_log_for_personal_values():
    text = (SANDBOX / "verify_phase2.sh").read_text(encoding="utf-8")
    assert "audit_tail.py" in text
    assert "--forbid" in text


def test_demo_runner_checks_the_audit_trail_it_shows():
    text = (DEMO / "run_injection_demo.sh").read_text(encoding="utf-8")
    assert "audit_tail.py" in text
    assert "expected at least 2 router denials" in text
    assert "forbidden" in text or "forbid" in text


def test_demo_is_clearly_synthetic():
    message = (DEMO / "untrusted_message.txt").read_text(encoding="utf-8")
    assert "synthetic" in message.lower()
    assert "collect.evil.example" in message
    record = (DEMO / "household_record.json").read_text(encoding="utf-8")
    assert "Devin Sullivan" in record
    assert "0847-4851" in record
