# Pick a Python that can run the router middleware tooling.
#
# Sourced by the Phase 2 scripts. The caller must set REPO_ROOT first.
# An explicit LEDGER_ROUTER_PYTHON always wins; otherwise prefer the
# repository venv, then the system interpreter. The probe needs grpc and
# the policy generator needs pydantic + PyYAML, so all three are checked.

if [ -n "${LEDGER_ROUTER_PYTHON:-}" ]; then
    ROUTER_PYTHON="${LEDGER_ROUTER_PYTHON}"
else
    ROUTER_PYTHON=""
    for _candidate in "${REPO_ROOT}/.venv/bin/python" python3 python; do
        if ! command -v "$_candidate" >/dev/null 2>&1; then
            continue
        fi
        if "$_candidate" -c 'import grpc, pydantic, yaml' >/dev/null 2>&1; then
            ROUTER_PYTHON="$_candidate"
            break
        fi
    done
    if [ -z "$ROUTER_PYTHON" ]; then
        ROUTER_PYTHON="python3"
        echo "warning: no Python with grpc + pydantic + PyYAML found." >&2
        echo "  Install the middleware extra: .venv/bin/pip install -e '.[middleware]'" >&2
        echo "  or set LEDGER_ROUTER_PYTHON=/path/to/python" >&2
    fi
fi
