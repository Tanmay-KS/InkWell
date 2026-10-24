#!/usr/bin/env bash
# Ledger — Phase 2 exit verification.
#
# One command for the Phase 2 exit condition:
#
#   * the privacy router is live and answers the OpenShell contract
#   * the sandbox policy carries the middleware
#   * egress is enforced for allowed host/path, unknown host, wrong path,
#     and wrong port
#   * the audit log records decisions without the personal values
#   * the prompt-injection demo is blocked on screen
#
# Run it on the gateway host, where the CLI holds the mTLS bundle and the
# middleware service is reachable. From another machine every check that
# needs the gateway is reported as SKIP/INCOMPLETE, not as a failure.
#
# Usage:
#   bash sandbox/verify_phase2.sh
#   bash sandbox/verify_phase2.sh --gateway hostfetch --no-demo
#   bash sandbox/verify_phase2.sh --audit-log /tmp/ledger-audit.jsonl
#
# Exit codes:
#   0  every required check passed
#   1  a required check failed
#   2  the gateway or sandbox could not be reached

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=/dev/null
. "${SCRIPT_DIR}/select_python.sh"
PROBE_PYTHON="$ROUTER_PYTHON"

SANDBOX_NAME="${LEDGER_SANDBOX_NAME:-ledger-hermes}"
GATEWAY_NAME="${LEDGER_GATEWAY_NAME:-}"
ROUTER_ENDPOINT="${LEDGER_ROUTER_ENDPOINT:-127.0.0.1:50051}"
AUDIT_LOG="${LEDGER_AUDIT_LOG:-}"
RUN_DEMO=1
DEMO_ACCOUNT="${LEDGER_DEMO_ACCOUNT:-0847-4851}"
DEMO_EMAIL="${LEDGER_DEMO_EMAIL:-devin.sullivan@example.com}"
DEMO_NAME="${LEDGER_DEMO_NAME:-Devin Sullivan}"
DEMO_ADDRESS="${LEDGER_DEMO_ADDRESS:-3482 Thistlewood Place}"
DEMO_PHONE="${LEDGER_DEMO_PHONE:-+1 437 126 5085}"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

while [ "$#" -gt 0 ]; do
    case "$1" in
        --gateway) GATEWAY_NAME="${2:-}"; shift 2 ;;
        --gateway=*) GATEWAY_NAME="${1#*=}"; shift ;;
        --sandbox) SANDBOX_NAME="${2:-}"; shift 2 ;;
        --sandbox=*) SANDBOX_NAME="${1#*=}"; shift ;;
        --audit-log) AUDIT_LOG="${2:-}"; shift 2 ;;
        --audit-log=*) AUDIT_LOG="${1#*=}"; shift ;;
        --endpoint) ROUTER_ENDPOINT="${2:-}"; shift 2 ;;
        --endpoint=*) ROUTER_ENDPOINT="${1#*=}"; shift ;;
        --no-demo) RUN_DEMO=0; shift ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -n "$GATEWAY_NAME" ]; then
    export OPENSHELL_GATEWAY="$GATEWAY_NAME"
fi
# Child probes use the environment, not the parent's local.
export LEDGER_SANDBOX_NAME="$SANDBOX_NAME"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac

RESULTS=()
PASSED=0
FAILED=0
SKIPPED=0

record() {
    local kind="$1" name="$2" status="$3" detail="${4:-}"
    local colour
    case "$status" in
        PASS) colour="$GREEN"; PASSED=$((PASSED + 1)) ;;
        SKIP) colour="$DIM"; SKIPPED=$((SKIPPED + 1)) ;;
        *)    colour="$RED"; FAILED=$((FAILED + 1)) ;;
    esac
    RESULTS+=("${kind}|${name}|${status}|${detail}")
    printf '  %-6s %s%-8s%s %s\n' "$kind" "$colour" "$status" "$RESET" "$name"
    [ -n "$detail" ] && printf '  %-6s %-8s %s%s%s\n' "" "" "$DIM" "$detail" "$RESET"
}

echo "${BOLD}Ledger — Phase 2 exit verification${RESET}"
echo "${DIM}sandbox: ${SANDBOX_NAME}${RESET}"
echo

if ! command -v openshell >/dev/null 2>&1; then
    echo "${RED}openshell is not on PATH${RESET}"
    echo "  See docs/phase0-runbook.md step 1."
    exit 2
fi

if ! openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME"; then
    echo "${YELLOW}The gateway is not reachable from this client, or ${SANDBOX_NAME} does not exist.${RESET}"
    echo "  Run this on the gateway host, or pass --gateway <name>."
    echo "  (From a remote client, Phase 0 verification already reports the same condition.)"
    exit 2
fi

# Prefer the audit path the applied policy names.
if [ -z "$AUDIT_LOG" ]; then
    AUDIT_LOG="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null \
        | grep -oE 'audit_log_path: *"?[^" ]+' | head -1 \
        | sed 's/.*: *"\?//')"
fi
AUDIT_LOG="${AUDIT_LOG:-/var/log/ledger/audit.jsonl}"

# 1. The contract the gateway validates at startup.
PROBE_OUT="$("$PROBE_PYTHON" "${SCRIPT_DIR}/middleware/ledger-router/probe.py" \
    --endpoint "$ROUTER_ENDPOINT" --self-test 2>&1)"
PROBE_RC=$?
if [ "$PROBE_RC" -eq 0 ]; then
    record required "middleware answers the supervisor contract and redacts" PASS \
        "$(printf '%s\n' "$PROBE_OUT" | tail -1)"
else
    record required "middleware answers the supervisor contract and redacts" FAIL \
        "$(printf '%s\n' "$PROBE_OUT" | tail -2 | tr '\n' ' ' | cut -c1-170)"
fi

# 2. The policy actually attaches it.
POLICY="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null)"
if printf '%s' "$POLICY" | grep -q 'ledger_router\|ledger-router'; then
    record required "policy carries the middleware entry" PASS "ledger_router"
else
    record required "policy carries the middleware entry" FAIL \
        "run: bash sandbox/router_on.sh"
fi

# 3. Egress enforcement, from inside the sandbox.
EGRESS_OUT="$(bash "${SCRIPT_DIR}/egress_probe.sh" --quiet 2>&1)"
EGRESS_RC=$?
if [ "$EGRESS_RC" -eq 0 ]; then
    record required "egress allow/deny boundaries hold" PASS \
        "5/5: allowed host/path reached; unknown host, wrong path and wrong port refused"
else
    record required "egress allow/deny boundaries hold" FAIL \
        "$(printf '%s\n' "$EGRESS_OUT" | tail -2 | tr '\n' ' ' | cut -c1-170)"
    printf '%s\n' "$EGRESS_OUT" | sed 's/^/        /'
fi

# 4. The OCSF JSON stream is what the architecture doc says it is.
SETTINGS="$(openshell settings get "$SANDBOX_NAME" 2>/dev/null)"
if printf '%s' "$SETTINGS" | grep -qi 'ocsf_json_enabled.*true'; then
    record info "OCSF JSON event stream is enabled" PASS ""
else
    record info "OCSF JSON event stream is enabled" FAIL \
        "run: openshell settings set ${SANDBOX_NAME} --key ocsf_json_enabled --value true"
fi

# 5. The demo, end to end.
if [ "$RUN_DEMO" -eq 1 ]; then
    DEMO_OUT="$(bash "${REPO_ROOT}/demo/injection/run_injection_demo.sh" \
        --sandbox "$SANDBOX_NAME" --audit-log "$AUDIT_LOG" 2>&1)"
    DEMO_RC=$?
    if [ "$DEMO_RC" -eq 0 ]; then
        record required "prompt-injection demo is blocked with an audit trail" PASS \
            "unknown host + two allowlisted-host exfiltration shapes refused"
    else
        record required "prompt-injection demo is blocked with an audit trail" FAIL \
            "exit ${DEMO_RC}"
        printf '%s\n' "$DEMO_OUT" | tail -20 | sed 's/^/        /'
    fi
else
    echo "  ${DIM}skip${RESET}   prompt-injection demo (--no-demo)"
fi

# 6. The audit trail is present, has router decisions, and does not store
#    the synthetic values.
if [ -f "$AUDIT_LOG" ] && [ -s "$AUDIT_LOG" ]; then
    ROUTER_EVENTS="$(grep -cE '"event_type"[[:space:]]*:[[:space:]]*"router\.' "$AUDIT_LOG" 2>/dev/null || true)"
    if [ "${ROUTER_EVENTS:-0}" -lt 1 ]; then
        record required "audit log records router decisions" FAIL \
            "no router.* events in ${AUDIT_LOG}"
    elif ! "$PROBE_PYTHON" "${SCRIPT_DIR}/audit_tail.py" --path "$AUDIT_LOG" \
        --forbid "$DEMO_ACCOUNT" --forbid "$DEMO_EMAIL" \
        --forbid "$DEMO_NAME" --forbid "$DEMO_ADDRESS" \
        --forbid "$DEMO_PHONE" >/dev/null 2>&1; then
        record required "audit log does not store personal values" FAIL \
            "the audit log contains a demo value — stop and inspect it"
    else
        record required "audit log records router decisions without the values" PASS \
            "${ROUTER_EVENTS} router event(s) in ${AUDIT_LOG}"
    fi
else
    record required "audit log records router decisions" FAIL \
        "no audit log at ${AUDIT_LOG} — check the policy's audit_log_path"
fi

echo
echo "${BOLD}Phase 2 exit condition${RESET}"
echo
printf '  %-6s %-8s %s\n' "KIND" "STATUS" "CHECK"
printf '  %-6s %-8s %s\n' "------" "--------" "-----------------------------"
for row in "${RESULTS[@]}"; do
    kind="${row%%|*}"; row="${row#*|}"
    name="${row%%|*}"; row="${row#*|}"
    status="${row%%|*}"; detail="${row#*|}"
    case "$status" in
        PASS) colour="$GREEN" ;; SKIP) colour="$DIM" ;; *) colour="$RED" ;;
    esac
    printf '  %-6s %s%-8s%s %s\n' "$kind" "$colour" "$status" "$RESET" "$name"
    [ -n "$detail" ] && printf '  %-6s %-8s %s%s%s\n' "" "" "$DIM" "$detail" "$RESET"
done
echo

if [ "$FAILED" -gt 0 ]; then
    printf '  %s%sFAIL%s  %d required check(s) failed, %d passed, %d skipped.\n' \
        "$RED" "$BOLD" "$RESET" "$FAILED" "$PASSED" "$SKIPPED"
    exit 1
fi
if [ "$SKIPPED" -gt 0 ]; then
    printf '  %s%sINCOMPLETE%s  %d passed, %d skipped.\n' \
        "$YELLOW" "$BOLD" "$RESET" "$PASSED" "$SKIPPED"
    exit 1
fi
printf '  %s%sPASS%s  %d check(s) passed.\n' "$GREEN" "$BOLD" "$RESET" "$PASSED"
if [ "$RUN_DEMO" -eq 1 ]; then
    printf '  %sThe router is enforcing, logging, and blocking the injection scenario.%s\n' \
        "$GREEN" "$RESET"
else
    printf '  %sThe router is enforcing and logging (injection demo skipped).%s\n' \
        "$GREEN" "$RESET"
fi
exit 0
