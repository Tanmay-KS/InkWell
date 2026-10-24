#!/usr/bin/env bash
# Run the Ledger privacy router middleware.
#
# The router has to be reachable from BOTH the gateway host and the sandbox
# supervisors. OpenShell's registration points at
# `host.openshell.internal:50051`, so the service binds 0.0.0.0 by default.
#
# Usage:
#   bash sandbox/middleware/ledger-router/run.sh
#   LEDGER_ROUTER_PORT=50051 bash sandbox/middleware/ledger-router/run.sh
#
# The audit log directory must exist and be writable by the user running
# this process. It is a host path, not a sandbox path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

cd "${SCRIPT_DIR}"

if [ -f "${REPO_ROOT}/.env" ]; then
    # Only the Token Factory key is needed, and only when the policy sets
    # `escalate: true`. Sourcing the file keeps the key out of the command
    # line; it is never printed.
    set -a
    # shellcheck disable=SC1091
    . "${REPO_ROOT}/.env"
    set +a
fi

PYTHON="${LEDGER_ROUTER_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
if [ ! -x "${PYTHON}" ]; then
    PYTHON="python3"
fi

AUDIT_LOG="${LEDGER_ROUTER_AUDIT_LOG:-/var/log/ledger/audit.jsonl}"
AUDIT_DIR="$(dirname "${AUDIT_LOG}")"
if [ -d "${AUDIT_DIR}" ] && [ -w "${AUDIT_DIR}" ]; then
    :
else
    echo "warning: ${AUDIT_DIR} is not writable; set LEDGER_ROUTER_AUDIT_LOG to a writable path" >&2
fi

export PYTHONPATH="${REPO_ROOT}:${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON}" -m ledger_router \
    --bind "${LEDGER_ROUTER_BIND:-0.0.0.0}" \
    --port "${LEDGER_ROUTER_PORT:-50051}" \
    --max-workers "${LEDGER_ROUTER_WORKERS:-8}" \
    --max-payload-bytes "${LEDGER_ROUTER_MAX_PAYLOAD_BYTES:-4194304}" \
    --audit-log "${AUDIT_LOG}"
