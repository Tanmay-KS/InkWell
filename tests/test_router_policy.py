"""The router-enabled policy is generated from the base policy.

The base file must stay applyable when the service is down, so the Phase 2
entry is generated rather than committed. These tests pin the two properties
that make that safe: the generated entry validates against the same schema
the service uses, and `--off` reproduces the base policy exactly.
"""

from __future__ import annotations

from pathlib import Path

import router_policy
import yaml
from ledger_router.config import RouterConfig

SANDBOX = Path(__file__).resolve().parent.parent / "sandbox"


def base_policy() -> dict:
    return yaml.safe_load((SANDBOX / "policy.yaml").read_text(encoding="utf-8"))


def test_off_reproduces_the_base_policy():
    policy = router_policy.build_policy(
        base_policy(), enabled=False, audit_log="/tmp/ledger-audit.jsonl"
    )
    assert policy == base_policy()
    assert "network_middlewares" not in policy


def test_on_adds_one_middleware_entry_with_the_expected_contract():
    policy = router_policy.build_policy(
        base_policy(), enabled=True, audit_log="/tmp/ledger-audit.jsonl"
    )
    entry = policy["network_middlewares"]["ledger_router"]
    assert entry["middleware"] == "ledger-router"
    assert entry["on_error"] == "fail_closed"
    assert entry["order"] == 10
    assert entry["config"]["escalate"] is False
    assert entry["config"]["audit_log_path"] == "/tmp/ledger-audit.jsonl"


def test_the_generated_config_is_accepted_by_the_service_schema():
    policy = router_policy.build_policy(
        base_policy(), enabled=True, audit_log="/tmp/ledger-audit.jsonl"
    )
    config = policy["network_middlewares"]["ledger_router"]["config"]
    parsed = RouterConfig(**config)
    assert parsed.audit_log_path == "/tmp/ledger-audit.jsonl"
    assert parsed.escalate is False
    assert parsed.deny_threshold >= 1


def test_the_middleware_only_attaches_to_hosts_it_allows():
    entry = router_policy.build_router_entry("/tmp/ledger-audit.jsonl")
    assert set(entry["endpoints"]["include"]) <= set(entry["config"]["allowed_hosts"])


def test_the_base_network_rules_are_unchanged():
    base = base_policy()
    policy = router_policy.build_policy(
        base, enabled=True, audit_log="/tmp/ledger-audit.jsonl"
    )
    assert policy["network_policies"] == base["network_policies"]
    assert policy["filesystem_policy"] == base["filesystem_policy"]
    assert policy["process"] == base["process"]


def test_the_audit_log_override_reaches_the_config():
    entry = router_policy.build_router_entry("/var/log/ledger/other.jsonl")
    assert entry["config"]["audit_log_path"] == "/var/log/ledger/other.jsonl"


def test_the_cli_writes_parseable_yaml(tmp_path, capsys):
    output = tmp_path / "policy.yaml"
    assert router_policy.main(["--on", "--output", str(output)]) == 0
    capsys.readouterr()
    generated = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert "network_middlewares" in generated
    assert generated["version"] == base_policy()["version"]


def test_the_check_mode_does_not_need_an_output_file(capsys):
    assert router_policy.main(["--check"]) == 0
    assert "valid" in capsys.readouterr().out


def test_off_and_on_are_mutually_exclusive():
    import pytest

    with pytest.raises(SystemExit):
        router_policy.main(["--on", "--off"])
