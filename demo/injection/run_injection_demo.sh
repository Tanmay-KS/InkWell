#!/usr/bin/env bash
# InkWell — live prompt-injection-block demo.
#
# The scenario: the agent reads an untrusted message that tells it to send
# the household record to an attacker. The script executes the message's
# instructions inside the real sandbox. Nothing here simulates the block:
# OpenShell's network policy refuses the unknown host, and the supervisor
# middleware refuses the two allowlisted-host exfiltration shapes.
#
# Usage:
#   bash demo/injection/run_injection_demo.sh
#   bash demo/injection/run_injection_demo.sh --gateway hostfetch
#   bash demo/injection/run_injection_demo.sh --audit-log /tmp/inkwell-audit.jsonl
#
# Exit codes:
#   0  all four attempts behaved as expected and the audit log recorded them
#   1  an attempt was not blocked, or the audit log is missing/unsafe
#   2  the demo could not run (no gateway or sandbox)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

SANDBOX_NAME="${INKWELL_SANDBOX_NAME:-inkwell-hermes}"
GATEWAY_NAME="${INKWELL_GATEWAY_NAME:-}"
AUDIT_LOG="${INKWELL_AUDIT_LOG:-}"
ACCOUNT="${INKWELL_DEMO_ACCOUNT:-0847-4851}"
EMAIL="${INKWELL_DEMO_EMAIL:-devin.sullivan@example.com}"
NAME="${INKWELL_DEMO_NAME:-Devin Sullivan}"
ADDRESS="${INKWELL_DEMO_ADDRESS:-3482 Thistlewood Place}"
PHONE="${INKWELL_DEMO_PHONE:-+1 437 126 5085}"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

while [ "$#" -gt 0 ]; do
    case "$1" in
        --gateway) GATEWAY_NAME="${2:-}"; shift 2 ;;
        --gateway=*) GATEWAY_NAME="${1#*=}"; shift ;;
        --sandbox) SANDBOX_NAME="${2:-}"; shift 2 ;;
        --sandbox=*) SANDBOX_NAME="${1#*=}"; shift ;;
        --audit-log) AUDIT_LOG="${2:-}"; shift 2 ;;
        --audit-log=*) AUDIT_LOG="${1#*=}"; shift ;;
        -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -n "$GATEWAY_NAME" ]; then
    export OPENSHELL_GATEWAY="$GATEWAY_NAME"
fi

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac

if ! command -v openshell >/dev/null 2>&1; then
    echo "openshell is not on PATH" >&2
    exit 2
fi

if ! openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME"; then
    echo "${YELLOW}Cannot run: sandbox ${SANDBOX_NAME} is not visible from this client.${RESET}" >&2
    echo "  Run this on the gateway host, or pass --gateway <name>." >&2
    exit 2
fi

# Prefer the path the applied policy actually names.
if [ -z "$AUDIT_LOG" ]; then
    AUDIT_LOG="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null \
        | grep -oE 'audit_log_path: *"?[^" ]+' | head -1 \
        | sed 's/.*: *"\?//')"
fi
AUDIT_LOG="${AUDIT_LOG:-/var/log/inkwell/audit.jsonl}"

echo "${BOLD}InkWell — prompt-injection demo${RESET}"
echo "${DIM}sandbox: ${SANDBOX_NAME}${RESET}"
echo "${DIM}audit:   ${AUDIT_LOG}${RESET}"
echo

# The audit log is written by the middleware on this host. If its directory
# is not writable, the demo will show a block but not the audit trail; say so
# before the run rather than after.
AUDIT_DIR="$(dirname "$AUDIT_LOG")"
if ! mkdir -p "$AUDIT_DIR" 2>/dev/null || [ ! -w "$AUDIT_DIR" ]; then
    echo "${YELLOW}warning: ${AUDIT_DIR} is not writable.${RESET}"
    echo "  The audit section will be empty. Set --audit-log to a writable path"
    echo "  that matches the service's policy config."
    echo
fi

BEFORE=0
if [ -f "$AUDIT_LOG" ]; then
    BEFORE="$(wc -l < "$AUDIT_LOG" | tr -d ' ')"
fi

echo "${BOLD}1. Upload the untrusted message and the agent's attempt script${RESET}"
openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
    /bin/mkdir -p /sandbox/injection >/dev/null 2>&1 || {
    echo "${RED}could not create /sandbox/injection in the sandbox${RESET}" >&2
    exit 2
}
for file in attempt_exfil.py household_record.json untrusted_message.txt; do
    if ! openshell sandbox upload "$SANDBOX_NAME" "${SCRIPT_DIR}/${file}" \
        "/sandbox/injection/${file}" >/dev/null 2>&1; then
        echo "${RED}could not upload ${file}${RESET}" >&2
        exit 2
    fi
done
echo "  ${GREEN}ok${RESET}  /sandbox/injection/{attempt_exfil.py,household_record.json,untrusted_message.txt}"
echo

echo "${BOLD}2. The agent follows the untrusted instructions${RESET}"
DEMO_OUTPUT="$(timeout 120s openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
    /usr/bin/python3 /sandbox/injection/attempt_exfil.py 2>&1)"
DEMO_RC=$?
printf '%s\n' "$DEMO_OUTPUT" | sed 's/^/  /'
echo

echo "${BOLD}3. What the runtime recorded${RESET}"
AUDIT_OK=1
if [ -f "$AUDIT_LOG" ]; then
    python3 "${REPO_ROOT}/sandbox/audit_tail.py" --path "$AUDIT_LOG" \
        --since-line "$BEFORE" --last 10 2>&1 | sed 's/^/  /'
    NEW_LINES="$(tail -n "+$((BEFORE + 1))" "$AUDIT_LOG" 2>/dev/null || true)"
    DENIES="$(printf '%s\n' "$NEW_LINES" | grep -cE '"decision"[[:space:]]*:[[:space:]]*"deny"' || true)"
    ALLOWS="$(printf '%s\n' "$NEW_LINES" | grep -cE '"decision"[[:space:]]*:[[:space:]]*"allow"' || true)"
    REDACTED="$(printf '%s\n' "$NEW_LINES" | grep -cE '"event_type"[[:space:]]*:[[:space:]]*"router\.redacted"' || true)"
    echo
    echo "  router denials:  ${DENIES}"
    echo "  router allows:   ${ALLOWS}"
    echo "  router redacts:  ${REDACTED}"
    if [ "${DENIES:-0}" -lt 2 ]; then
        echo "  ${RED}expected at least 2 router denials${RESET}"
        AUDIT_OK=0
    fi
    if [ "${ALLOWS:-0}" -lt 1 ]; then
        echo "  ${RED}expected the benign control to be allowed${RESET}"
        AUDIT_OK=0
    fi
    # The audit trail is safe to show only if it never stored the values.
    if ! python3 "${REPO_ROOT}/sandbox/audit_tail.py" --path "$AUDIT_LOG" \
        --forbid "$ACCOUNT" --forbid "$EMAIL" --forbid "$NAME" \
        --forbid "$ADDRESS" --forbid "$PHONE" >/dev/null 2>&1; then
        echo "  ${RED}the audit log contains a synthetic personal value${RESET}"
        AUDIT_OK=0
    fi
else
    echo "  ${YELLOW}no audit log at ${AUDIT_LOG}${RESET}"
    AUDIT_OK=0
fi
echo

echo "${BOLD}4. The network policy's own record${RESET}"
LOG_MATCH="$(openshell logs "$SANDBOX_NAME" --since 5m --source gateway --level debug 2>/dev/null \
    | grep -iE 'collect\.evil\.example|policy_denied|network_policy_denied|denied' \
    | tail -5)"
if [ -n "$LOG_MATCH" ]; then
    printf '%s\n' "$LOG_MATCH" | sed 's/^/  /'
else
    echo "  ${DIM}(no matching gateway line yet — the block is still proven by the client side)${RESET}"
    echo "  ${DIM}try: openshell logs ${SANDBOX_NAME} --since 5m --source all${RESET}"
fi
echo

if [ "$DEMO_RC" -ne 0 ]; then
    echo "${RED}${BOLD}FAIL${RESET}  an exfiltration attempt was not stopped as expected."
    exit 1
fi
if [ "$AUDIT_OK" -ne 1 ]; then
    echo "${RED}${BOLD}FAIL${RESET}  the block happened, but the audit trail is incomplete or unsafe."
    exit 1
fi

echo "${GREEN}${BOLD}DEMO PASS${RESET}"
echo "  The unknown host was refused by the network policy."
echo "  The allowlisted-host exfiltration attempts were refused by the router."
echo "  The benign search was allowed."
echo "  The audit trail recorded the decisions without the personal values."
