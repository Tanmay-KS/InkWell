"""Regression tests for the sandbox configuration.

These are the checks that catch the mistakes the Phase 0 research actually
flagged as likely: a provider host that does not match what Hermes calls, a
root workload identity, a policy that references a host no profile binds.

They read the real files, so they fail the moment a file drifts from what
the code assumes.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "sandbox"
PROVIDERS = SANDBOX / "providers"

# Hermes's built-in nebius-token-factory provider pins this base URL
# (plugins/model-providers/nebius-token-factory/__init__.py in the Hermes
# repository). The OpenShell provider profile must name the same host, or
# every model call from inside the sandbox fails with
# credential_endpoint_mismatch.
HERMES_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"

# The upstream Hermes image creates its runtime user with this UID
# (`useradd -u 10000 -m -d /opt/data hermes`).
HERMES_UID = 10000


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_policy_declares_version_1():
    policy = load_yaml(SANDBOX / "policy.yaml")
    assert policy["version"] == 1


def test_gateway_declares_schema_version_2():
    config = tomllib.loads((SANDBOX / "gateway.toml").read_text(encoding="utf-8"))
    assert config["openshell"]["version"] == 2
    assert config["openshell"]["gateway"]["compute_driver"] == "docker"


def test_gateway_middleware_is_not_registered_before_phase_2():
    """A registered middleware service that is down stops the gateway.

    The router does not exist until Phase 2, so the registration must stay
    commented out until then. This test fails loudly if someone uncomments
    it before the service is up.
    """
    raw = (SANDBOX / "gateway.toml").read_text(encoding="utf-8")
    active = [
        line
        for line in raw.splitlines()
        if line.strip().startswith("[[openshell.supervisor.middleware]]")
    ]
    assert not active, (
        "The middleware registration is active but the ledger-router service "
        "does not exist until Phase 2. The gateway will refuse to start."
    )


def test_policy_runs_as_a_non_root_user():
    policy = load_yaml(SANDBOX / "policy.yaml")
    process = policy["process"]
    run_as_user = str(process["run_as_user"])
    assert run_as_user not in ("0", "root"), "OpenShell rejects a root workload"
    assert int(run_as_user) == HERMES_UID


def test_policy_keeps_the_baseline_filesystem_paths():
    """`policy set` replaces every section, and OpenShell can reject a
    replacement that removes the baseline paths it added at startup."""
    policy = load_yaml(SANDBOX / "policy.yaml")
    fs = policy["filesystem_policy"]
    read_only = set(fs.get("read_only", []))
    read_write = set(fs.get("read_write", []))

    for path in ("/usr", "/lib", "/etc", "/var/log", "/proc", "/dev/urandom"):
        assert path in read_only, f"{path} is a baseline read-only path"
    for path in ("/tmp", "/dev/null"):
        assert path in read_write, f"{path} is a baseline read-write path"

    # The Hermes install tree is immutable; the data volume is not.
    assert "/opt/hermes" in read_only
    assert "/opt/data" in read_write


def test_every_profile_has_the_required_shape():
    profiles = sorted(PROVIDERS.glob("*.yaml"))
    assert profiles, "no provider profiles found"

    for path in profiles:
        profile = load_yaml(path)
        assert "id" in profile, f"{path.name} has no id"
        assert profile.get("category") in {
            "other",
            "inference",
            "agent",
            "source_control",
            "messaging",
            "data",
            "knowledge",
        }, f"{path.name} has an invalid category"

        credentials = profile.get("credentials") or []
        assert credentials, f"{path.name} declares no credentials"
        for credential in credentials:
            assert credential.get("env_vars"), f"{path.name} has no env var"
            assert credential.get("auth_style") in {
                "basic",
                "bearer",
                "header",
                "query",
                "path",
            }

        endpoints = profile.get("endpoints") or []
        assert endpoints, f"{path.name} declares no endpoints"
        for endpoint in endpoints:
            assert endpoint.get("host"), f"{path.name} has a hostless endpoint"
            assert endpoint.get("port"), f"{path.name} has a portless endpoint"

        assert profile.get("binaries"), f"{path.name} declares no binaries"


def test_nebius_profile_host_matches_hermes():
    """The single most likely Phase 0 failure.

    If the OpenShell profile binds the credential to a different host than
    Hermes calls, every request is refused with
    credential_endpoint_mismatch.
    """
    profile = load_yaml(PROVIDERS / "nebius-token-factory.yaml")
    hosts = {endpoint["host"] for endpoint in profile["endpoints"]}
    assert hosts == {"api.tokenfactory.nebius.com"}

    expected_host = HERMES_NEBIUS_BASE_URL.split("/")[2]
    assert expected_host in hosts


def test_nebius_profile_uses_the_same_env_var_hermes_reads():
    """Hermes reads NEBIUS_API_KEY. The profile must inject the placeholder
    under the same name, or the agent never sees a credential at all."""
    profile = load_yaml(PROVIDERS / "nebius-token-factory.yaml")
    env_vars = {
        var for credential in profile["credentials"] for var in credential["env_vars"]
    }
    assert "NEBIUS_API_KEY" in env_vars


def test_policy_hosts_are_covered_by_a_profile():
    """Every host the policy allows must be a host some profile binds.

    Otherwise the policy permits traffic that no credential is bound to,
    which is how an accidental second destination gets in.
    """
    policy = load_yaml(SANDBOX / "policy.yaml")
    policy_hosts = {
        endpoint["host"]
        for rule in policy["network_policies"].values()
        for endpoint in rule["endpoints"]
    }

    profile_hosts: set[str] = set()
    for path in sorted(PROVIDERS.glob("*.yaml")):
        profile = load_yaml(path)
        for endpoint in profile.get("endpoints") or []:
            profile_hosts.add(endpoint["host"])

    assert policy_hosts, "the policy allows no network destinations"
    assert policy_hosts <= profile_hosts, (
        f"policy allows hosts with no provider profile: "
        f"{sorted(policy_hosts - profile_hosts)}"
    )


def test_policy_middleware_block_is_not_active_before_phase_2():
    """The policy names a middleware service that does not exist yet.

    OpenShell asks the gateway to validate a middleware service's config
    before it accepts a policy, so an active entry would make every policy
    update fail.
    """
    policy = load_yaml(SANDBOX / "policy.yaml")
    assert not policy.get("network_middlewares"), (
        "network_middlewares is active, but the ledger-router service does "
        "not exist until Phase 2 — the gateway will reject the policy."
    )


def test_policy_has_no_wildcard_hosts():
    """A wildcard host is a DNS-label exfiltration channel.

    OpenShell's own guidance is to prefer exact hostnames.
    """
    policy = load_yaml(SANDBOX / "policy.yaml")
    for rule in policy["network_policies"].values():
        for endpoint in rule["endpoints"]:
            host = endpoint["host"]
            assert "*" not in host, f"wildcard host in the policy: {host}"


@pytest.mark.parametrize("profile_path", sorted(PROVIDERS.glob("*.yaml")))
def test_profile_ids_are_kebab_case(profile_path: Path):
    """OpenShell requires lowercase kebab-case profile IDs."""
    profile = load_yaml(profile_path)
    profile_id = profile["id"]
    assert profile_id == profile_id.lower()
    assert all(c.isalnum() or c == "-" for c in profile_id), profile_id


# --- the verification script ------------------------------------------------


def test_verify_script_is_valid_bash():
    """A syntax error in the exit-condition script would make Phase 0
    unfalsifiable, so it is checked like any other code."""
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(SANDBOX / "verify.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_setup_script_is_valid_bash():
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(SANDBOX / "setup.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _verify_script_text() -> str:
    return (SANDBOX / "verify.sh").read_text(encoding="utf-8")


def test_negative_check_targets_a_policy_allowed_host():
    """The credential-scoping test is only meaningful if the host it
    targets is ALLOWED by the policy.

    A request to a host outside `network_policies` is denied at the policy
    layer and never reaches the credential binding — so the test would
    appear to pass while proving nothing. This is exactly the bug that
    was in the first version of the script.
    """
    text = _verify_script_text()
    assert 'UNBOUND_HOST="api.tavily.com"' in text, (
        "verify.sh must name the unbound host explicitly"
    )

    policy = load_yaml(SANDBOX / "policy.yaml")
    policy_hosts = {
        endpoint["host"]
        for rule in policy["network_policies"].values()
        for endpoint in rule["endpoints"]
    }
    assert "api.tavily.com" in policy_hosts, (
        "the host the negative check targets must be allowed by the policy, "
        "otherwise the check never reaches the credential binding"
    )


def test_negative_check_does_not_send_the_real_key():
    """The sandbox holds an opaque placeholder, not the real key.

    Interpolating the host's $NEBIUS_API_KEY into the command would put a
    live secret on a command line AND would not exercise the binding at
    all. The variable must be expanded inside the sandbox.
    """
    text = _verify_script_text()
    # The only NEBIUS_API_KEY reference must be inside an escaped
    # double-quoted command string, i.e. expanded by the sandbox shell.
    assert "Authorization: Bearer \\$NEBIUS_API_KEY" in text, (
        "the placeholder must be expanded inside the sandbox, not on the host"
    )
    assert "Bearer ${NEBIUS_API_KEY" not in text, (
        "the real key must never be interpolated into the sandbox command"
    )


def test_verifier_does_not_check_a_host_local_docker_daemon():
    """The gateway's compute driver is what matters, not this machine's Docker.

    A local `docker info` answers a different question: it is false for any
    client that is not the gateway host, and for a host whose Docker socket
    needs sudo — even when the gateway is perfectly able to create
    sandboxes.
    """
    # Only executable lines count; the script explains in a comment why it
    # does not do this.
    code = "\n".join(
        line
        for line in _verify_script_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "docker info" not in code, (
        "verify.sh must not gate on a host-local Docker daemon"
    )


def test_verifier_reports_unevaluable_checks_as_skip():
    """A check that could not be evaluated must not be reported as a failure.

    The verifier's whole job is to certify a phase. If it cannot reach the
    gateway it must say so once and skip the rest, not report a wall of
    failures that describe healthy infrastructure.
    """
    text = _verify_script_text()
    assert "SKIP" in text, "the verifier needs a third outcome besides PASS and FAIL"
    assert "skip_all_dependent" in text, (
        "dependent checks must be skipped when the gateway is unreachable"
    )
    assert "TRANSPORT_FAILURE" in text, (
        "the verifier must detect a transport failure separately from a check failure"
    )


def test_verifier_explains_a_client_certificate_failure():
    """A remote CLI cannot authenticate without the gateway's client bundle.

    The error it hits is `invalid peer certificate: BadSignature`, which
    looks like a broken TLS stack rather than a client-side credential
    problem. The verifier must name the remedy.
    """
    text = _verify_script_text()
    assert "BadSignature" in text or "peer certificate" in text, (
        "the verifier must recognise a client-certificate failure"
    )
    assert "gateways/" in text, (
        "the remedy must point at the gateway's client bundle"
    )


def test_verifier_can_target_a_named_gateway():
    """A remote client may hold a bundle under its own registration name."""
    text = _verify_script_text()
    assert "--gateway" in text, "the verifier must accept a gateway name"
    assert "OPENSHELL_GATEWAY" in text, (
        "the selected gateway must be passed through to the CLI"
    )


# --- the pipeline harness ---------------------------------------------------


def test_pipeline_harness_is_valid_bash():
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(SANDBOX / "run_pipeline.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pipeline_harness_uploads_without_git_ignore():
    """The synthetic corpus is gitignored.

    A git-filtered upload silently skips ignored files, so the document
    would never arrive and the harness would fail for a reason that has
    nothing to do with the pipeline.
    """
    text = (SANDBOX / "run_pipeline.sh").read_text(encoding="utf-8")
    assert "--no-git-ignore" in text, (
        "the upload must not be filtered by .gitignore — the corpus is ignored"
    )


def test_pipeline_harness_documents_its_entry_point_contract():
    """Person B codes against the harness, so the contract has to be in it.

    The pipeline is invoked as a command with one argument and prints one
    JSON object. Without that written down, "run it in the sandbox" is not
    actionable.
    """
    text = (SANDBOX / "run_pipeline.sh").read_text(encoding="utf-8")
    assert "LEDGER_PIPELINE_CMD" in text, (
        "the entry point must be overridable, so the harness works before the"
        " pipeline exists"
    )
    for key in ("deadline", "action", "extraction", "redaction", "egress"):
        assert f'"{key}"' in text, f"the documented JSON contract must include {key}"


def test_pipeline_harness_runs_inside_the_sandbox():
    """The whole point of the harness.

    The pipeline must be executed by the sandbox, not on the host — the
    credential placeholder, the closed filesystem and the three-host
    allowlist only exist inside.
    """
    text = (SANDBOX / "run_pipeline.sh").read_text(encoding="utf-8")
    assert "sandbox exec" in text, "the pipeline must be run via sandbox exec"
    assert "sandbox upload" in text, "the document must be uploaded into the sandbox"


def test_the_plumbing_check_accepts_a_reply_without_prose():
    """F-040: a reasoning model can answer with `content: null`.

    The probe once read `message.get("content", "").strip()`. `get` applies
    its default only when the key is *missing*, so a JSON `null` arrived as
    `None`, `.strip()` raised, and the enclosing `except` reported a
    successful call as "the sandbox cannot reach Token Factory" — the most
    misleading failure the harness can produce.

    A parsed response is the success condition. Empty prose is not a
    network fault.
    """
    text = (SANDBOX / "run_pipeline.sh").read_text(encoding="utf-8")

    assert '.get("content", "")' not in text, "the null-content bug, reintroduced"
    assert '(message.get("content") or "")' in text, "content may be null"
    assert '"MODEL-OK (no prose' in text, "empty content must still be a success"
    assert '"NoChoices"' in text, "no completion at all is the real failure"

