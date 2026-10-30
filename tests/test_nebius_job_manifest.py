"""Tests for Nebius job manifest and deployment script."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "jobs" / "nebius-job.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "jobs" / "deploy_nebius_job.sh"


def test_nebius_job_manifest_valid_yaml():
    assert MANIFEST_PATH.exists(), f"Manifest file missing at {MANIFEST_PATH}"

    with open(MANIFEST_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert data["apiVersion"] == "batch.nebius.ai/v1alpha1"
    assert data["kind"] == "Job"
    assert data["metadata"]["name"] == "inkwell-deadline-monitor"

    spec = data["spec"]
    assert "schedule" in spec
    container = spec["template"]["spec"]["containers"][0]
    assert container["name"] == "deadline-monitor"
    assert "ghcr.io" in container["image"]

    env_map = {item["name"]: item.get("value") for item in container.get("env", []) if "value" in item}
    assert env_map.get("INKWELL_MEMORY_DB") == "/data/inkwell_memory.db"
    assert env_map.get("INKWELL_STATE_PATH") == "/data/state/job_state.json"
    assert env_map.get("INKWELL_NOTIFY") == "1"

    mounts = container.get("volumeMounts", [])
    assert any(m["mountPath"] == "/data" for m in mounts)


def test_nebius_deploy_script_dry_run():
    assert DEPLOY_SCRIPT.exists()

    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--dry-run"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[DRY-RUN] Manifest verified" in result.stdout
    assert "nebius ai job create" in result.stdout
    assert "--name inkwell-deadline-monitor" in result.stdout


def test_nebius_deploy_script_trigger_dry_run():
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--trigger", "--dry-run"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[DRY-RUN] Would run: nebius ai job run create" in result.stdout
