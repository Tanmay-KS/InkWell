"""Offline checks for the deadline-monitor container image.

The real Docker build runs in GitHub Actions. These tests keep the Dockerfile,
the build context, and the runtime tree honest without requiring a Docker
daemon on the development host.

The staged-runtime tests copy exactly the files named by the Dockerfile into a
temporary directory and run them with ``python -S``. Disabling site-packages
means an accidental third-party import fails here rather than at image build
or, worse, at the first scheduled Nebius run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "jobs" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

EXPECTED_COPY_SOURCES = {
    "agent/__init__.py",
    "agent/memory/__init__.py",
    "agent/memory/hermes_memory.py",
    "jobs/__init__.py",
    "jobs/deadline_monitor.py",
    "jobs/notifier.py",
    "jobs/seed_due_item.py",
}

REQUIRED_IGNORE_PATTERNS = {
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    ".openshell/",
    "**/.openshell/",
    "sandbox/local/",
    "sandbox/host-bootstrap.sh",
    "*.local.toml",
    "**/*.local.toml",
    "openshell.db",
    "**/openshell.db",
    "tls/",
    "**/tls/",
    "*.pem",
    "**/*.pem",
    "*.key",
    "**/*.key",
    "*.crt",
    "**/*.crt",
    "*.p12",
    "**/*.p12",
    "*.pfx",
    "**/*.pfx",
    "credentials.json",
    "**/credentials.json",
    ".venv*",
    "**/.venv*",
}

COPY_RE = re.compile(r"^\s*(?P<instruction>COPY|ADD)\s+(?P<args>.+?)\s*$", re.IGNORECASE)


def _dockerfile_lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _copy_sources() -> list[str]:
    sources: list[str] = []
    for line in _dockerfile_lines():
        match = COPY_RE.match(line)
        if match is None:
            continue
        assert match.group("instruction").upper() == "COPY", f"ADD is not allowed: {line}"
        args = [part for part in match.group("args").split() if not part.startswith("--")]
        assert len(args) >= 2, f"COPY needs a source and destination: {line}"
        sources.extend(args[:-1])
    return sources


def _stage_image_tree(destination: Path) -> None:
    for relative in EXPECTED_COPY_SOURCES:
        source = REPO_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _run_staged(stage: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(stage)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("LEDGER_MEMORY_DB", None)
    environment.pop("LEDGER_STATE_PATH", None)
    return subprocess.run(
        [sys.executable, "-S", "-m", "jobs.deadline_monitor", *args],
        cwd=stage,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_dockerfile_copies_only_the_monitor_modules():
    sources = _copy_sources()

    assert set(sources) == EXPECTED_COPY_SOURCES
    for source in sources:
        assert source not in {".", "./", ".."}, f"COPY must not copy the whole context: {source}"
        assert not source.startswith("/"), f"COPY source must be relative: {source}"
        assert (REPO_ROOT / source).is_file(), f"COPY source is missing: {source}"


def test_dockerfile_does_not_put_secrets_in_the_image_environment():
    for line in _dockerfile_lines():
        if not line.upper().startswith(("ARG ", "ENV ")):
            continue
        assert not re.search(
            r"(?i)(api[_-]?key|secret|password|token)\s*=", line
        ), f"secret-like build configuration: {line}"


def test_dockerfile_runs_unprivileged_with_the_expected_entrypoint():
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in text
    assert "USER 10000:10000" in text
    assert 'ENTRYPOINT ["python", "-m", "jobs.deadline_monitor"]' in text
    assert "LEDGER_MEMORY_DB=/data/ledger_memory.db" in text
    assert "LEDGER_STATE_PATH=/data/state/job_state.json" in text


def test_dockerignore_excludes_credentials_and_host_state():
    patterns = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    missing = REQUIRED_IGNORE_PATTERNS - patterns
    assert not missing, f".dockerignore is missing: {sorted(missing)}"


def test_staged_image_tree_has_no_secret_bearing_files():
    for relative in EXPECTED_COPY_SOURCES:
        path = Path(relative)
        assert path.name not in {".env", "credentials.json"}
        assert path.suffix not in {".pem", ".key", ".crt", ".p12", ".pfx"}


def test_staged_image_tree_runs_without_site_packages(tmp_path):
    stage = tmp_path / "app"
    _stage_image_tree(stage)

    result = _run_staged(stage, "--help")

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_staged_image_tree_runs_a_dry_run(tmp_path):
    stage = tmp_path / "app"
    _stage_image_tree(stage)

    result = _run_staged(
        stage,
        "--dry-run",
        "--today",
        "2026-10-20",
        "--memory-db",
        str(tmp_path / "memory.db"),
        "--state-path",
        str(tmp_path / "state.json"),
    )

    assert result.returncode == 0, result.stderr
    assert "flagged 0 item(s)" in result.stdout
    assert "Traceback" not in result.stderr


def test_staged_image_tree_keeps_personal_fields_out_of_logs(tmp_path):
    stage = tmp_path / "app"
    _stage_image_tree(stage)
    database = tmp_path / "memory.db"
    seed = (
        "from datetime import date\n"
        "from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory\n"
        f"memory = HouseholdMemory({str(database)!r})\n"
        "memory.upsert(HouseholdItem(\n"
        "    item_id='item-container-smoke',\n"
        "    kind='subscription',\n"
        "    provider='Acme',\n"
        "    holder='Devin Sullivan',\n"
        "    reference='REF-123',\n"
        "    renewal_date=date(2026, 10, 23),\n"
        "))\n"
        "memory.close()\n"
    )
    seeded = subprocess.run(
        [sys.executable, "-S", "-c", seed],
        cwd=stage,
        env={**os.environ, "PYTHONPATH": str(stage)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert seeded.returncode == 0, seeded.stderr

    result = _run_staged(
        stage,
        "--dry-run",
        "--today",
        "2026-10-20",
        "--memory-db",
        str(database),
        "--state-path",
        str(tmp_path / "state.json"),
    )

    assert result.returncode == 0, result.stderr
    assert "flagged 1 item(s)" in result.stdout
    assert "Devin Sullivan" not in result.stdout
    assert "REF-123" not in result.stdout
    assert "Devin Sullivan" not in result.stderr
    assert "REF-123" not in result.stderr
