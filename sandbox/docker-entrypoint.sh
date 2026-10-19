#!/bin/sh
# Ledger — direct Hermes entrypoint for the OpenShell sandbox.
#
# WHY THIS EXISTS
# ---------------
# The upstream Hermes image ships an s6-overlay entrypoint that expects to
# be PID 1 as root: /init owns the supervision tree, and the stage2 hook
# chowns the data volume and drops privileges per service.
#
# Under OpenShell that model does not apply:
#
#   * OpenShell's Docker driver runs the workload as the policy's
#     run_as_user (we pin UID 10000, the image's `hermes` account).
#   * OpenShell REJECTS an image whose USER is root unless the creation
#     policy names a non-root user.
#   * OpenShell already provides process supervision at the sandbox level
#     (--restart-policy), so s6's per-service supervision is redundant.
#
# So we exec the Hermes CLI directly. The upstream dispatcher would have
# tried /init first and fallen back only when it is NOT PID 1 — and the
# fallback still runs the stage2 hook, which wants root.
#
# The upstream dispatcher also owned the argument routing (a bare executable
# on PATH is exec'd directly, anything else becomes `hermes <args>`). We
# reproduce that here so `openshell sandbox create -- chat -q "hello"` does
# the obvious thing.
#
# The venv binary is invoked by ABSOLUTE PATH, and the upstream
# /opt/hermes/bin/hermes shim is deliberately NOT used: that shim re-execs
# through s6-setuidgid when called as root, and it hard-fails (exit 126)
# when s6-setuidgid is missing — which is exactly our situation.
set -e

HERMES_BIN=/opt/hermes/.venv/bin/hermes

if [ ! -x "$HERMES_BIN" ]; then
    echo "[ledger] FATAL: $HERMES_BIN is missing or not executable." >&2
    echo "[ledger] The base image must be nousresearch/hermes-agent (or a derivative of it)." >&2
    exit 127
fi

if [ "$#" -eq 0 ]; then
    exec "$HERMES_BIN" chat
fi

# First argument is an executable on PATH -> run it directly.
# Otherwise -> treat the whole argument list as a Hermes subcommand.
case "$1" in
    /*|/*/*)
        exec "$@"
        ;;
esac

if command -v "$1" >/dev/null 2>&1; then
    exec "$@"
fi

exec "$HERMES_BIN" "$@"
