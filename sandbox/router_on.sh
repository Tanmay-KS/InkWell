#!/usr/bin/env bash
# Attach the Ledger privacy router to a live sandbox.
#
# Prerequisites, in this order:
#   1. the middleware service is running and reachable from the gateway host
#   2. the gateway has the service registered (sandbox/router_register.sh)
#   3. this script generates the router-enabled policy from sandbox/policy.yaml
#      and applies it with --wait
#
# If the policy is refused, the base policy is re-applied immediately: the
# gateway's default failure mode blocks traffic until a valid policy loads,
# so leaving a rejected policy in place would be worse than not starting.
#
# Usage:
#   bash sandbox/router_on.sh
#   bash sandbox/router_on.sh --gateway hostfetch --audit-log /tmp/ledger-audit.jsonl
#   bash sandbox/router_on.sh --escalate          # needs NEBIUS_API_KEY on the service host

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=/dev/null
. "${SCRIPT_DIR}/select_python.sh"
PROBE_PYTHON="$ROUTER_PYTHON"

SANDBOX_NAME="${LEDGER_SANDBOX_NAME:-ledger-hermes}"
GATEWAY_NAME="${LEDGER_GATEWAY_NAME:-}"
AUDIT_LOG="${LEDGER_AUDIT_LOG:-/var/log/ledger/audit.jsonl}"
ROUTER_ENDPOINT="${LEDGER_ROUTER_ENDPOINT:-127.0.0.1:50051}"
ESCALATE=0
SKIP_PROBE=0

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
        --escalate) ESCALATE=1; shift ;;
        --skip-probe) SKIP_PROBE=1; shift ;;
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

die() { echo "${RED}${*}${RESET}" >&2; exit 1; }

command -v openshell >/dev/null 2>&1 || die "openshell is not on PATH"
openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME" \
    || die "sandbox ${SANDBOX_NAME} is not visible from this client"

AUDIT_DIR="$(dirname "$AUDIT_LOG")"
mkdir -p "$AUDIT_DIR" 2>/dev/null || die "cannot create the audit directory ${AUDIT_DIR}"
[ -w "$AUDIT_DIR" ] || die "the audit directory ${AUDIT_DIR} is not writable by this user"

if [ "$SKIP_PROBE" -eq 0 ]; then
    echo "${BOLD}1. The middleware service${RESET}"
    if ! "$PROBE_PYTHON" "${SCRIPT_DIR}/middleware/ledger-router/probe.py" \
        --endpoint "$ROUTER_ENDPOINT" --self-test; then
        die "the router service is not healthy at ${ROUTER_ENDPOINT} — start it with sandbox/middleware/ledger-router/run.sh"
    fi
    echo
else
    echo "${YELLOW}skipping the service probe (--skip-probe)${RESET}"
fi

TMP_POLICY="$(mktemp "${TMPDIR:-/tmp}/ledger-policy-router-XXXXXX.yaml")"
trap 'rm -f "$TMP_POLICY"' EXIT

echo "${BOLD}2. Generate the router-enabled policy${RESET}"
ROUTER_ARGS=(--on --audit-log "$AUDIT_LOG" --output "$TMP_POLICY")
[ "$ESCALATE" -eq 1 ] && ROUTER_ARGS+=(--escalate)
"$PROBE_PYTHON" "${SCRIPT_DIR}/router_policy.py" "${ROUTER_ARGS[@]}" \
    || die "could not generate the policy"
echo "  ${GREEN}ok${RESET}  ${TMP_POLICY}"
echo

echo "${BOLD}3. Apply the policy${RESET}"
if openshell policy set "$SANDBOX_NAME" --policy "$TMP_POLICY" --wait; then
    echo "  ${GREEN}ok${RESET}  policy loaded"
else
    echo "  ${RED}the gateway refused the policy${RESET}"
    echo "  re-applying the base policy so the sandbox is not left blocked..."
    if openshell policy set "$SANDBOX_NAME" --policy "${SCRIPT_DIR}/policy.yaml" --wait; then
        echo "  ${GREEN}ok${RESET}  base policy restored"
    else
        echo "  ${RED}${BOLD}WARNING${RESET}${RED}: the base policy also failed to load.${RESET}"
        echo "  The sandbox may have no egress until a valid policy is applied."
    fi
    echo
    echo "  Common causes:"
    echo "    - the gateway does not have the middleware registered:"
    echo "        bash sandbox/router_register.sh"
    echo "    - the service is not reachable from the sandbox supervisor:"
    echo "        host.openshell.internal:50051 from inside the sandbox"
    echo "    - the router config was rejected:"
    echo "        ${PROBE_PYTHON} sandbox/router_policy.py --check"
    exit 1
fi
echo

echo "${BOLD}4. Turn on the JSON event stream${RESET}"
if openshell settings set "$SANDBOX_NAME" --key ocsf_json_enabled --value true >/dev/null 2>&1; then
    echo "  ${GREEN}ok${RESET}  ocsf_json_enabled=true"
else
    echo "  ${YELLOW}could not set ocsf_json_enabled; the router's own JSONL is still written${RESET}"
fi
echo

if ! EFFECTIVE_POLICY="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null)"; then
    echo "  ${RED}could not read the effective policy${RESET}" >&2
    echo "  Check: openshell policy get ${SANDBOX_NAME} --full" >&2
    exit 1
fi
if printf '%s' "$EFFECTIVE_POLICY" | grep -q 'ledger_router\|ledger-router'; then
    echo "  ${GREEN}${BOLD}ROUTER ON${RESET}  ${SANDBOX_NAME}"
else
    echo "  ${RED}policy applied, but the effective policy does not show the middleware entry${RESET}" >&2
    echo "  Check: openshell policy get ${SANDBOX_NAME} --full" >&2
    exit 1
fi

echo
echo "${BOLD}Next${RESET}"
echo "  bash sandbox/verify_phase2.sh --audit-log ${AUDIT_LOG}"
echo "  bash demo/injection/run_injection_demo.sh --audit-log ${AUDIT_LOG}"
