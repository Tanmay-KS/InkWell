#!/usr/bin/env bash
# Detach the InkWell privacy router from a live sandbox.
#
# Applies the base policy (no `network_middlewares` entry) with --wait and
# verifies that the middleware is gone from the effective policy. The service
# keeps running: this is the router OFF leg of the leak test, not a teardown.
#
# Usage:
#   bash sandbox/router_off.sh
#   bash sandbox/router_off.sh --gateway hostfetch
#
# If you want to remove the gateway registration too, run
# `bash sandbox/router_unregister.sh` after this.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SANDBOX_NAME="${INKWELL_SANDBOX_NAME:-inkwell-hermes}"
GATEWAY_NAME="${INKWELL_GATEWAY_NAME:-}"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'

while [ "$#" -gt 0 ]; do
    case "$1" in
        --gateway) GATEWAY_NAME="${2:-}"; shift 2 ;;
        --gateway=*) GATEWAY_NAME="${1#*=}"; shift ;;
        --sandbox) SANDBOX_NAME="${2:-}"; shift 2 ;;
        --sandbox=*) SANDBOX_NAME="${1#*=}"; shift ;;
        -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
    echo "${RED}openshell is not on PATH${RESET}" >&2
    exit 2
fi
if ! openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME"; then
    echo "${RED}sandbox ${SANDBOX_NAME} is not visible from this client${RESET}" >&2
    exit 2
fi

echo "${BOLD}Router OFF — applying the base policy${RESET}"
if ! openshell policy set "$SANDBOX_NAME" --policy "${SCRIPT_DIR}/policy.yaml" --wait; then
    echo "${RED}the base policy did not load${RESET}" >&2
    echo "  Check: openshell policy list ${SANDBOX_NAME}" >&2
    exit 1
fi

if ! EFFECTIVE_POLICY="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null)"; then
    echo "${RED}could not read the effective policy; cannot confirm the router is detached${RESET}" >&2
    echo "  Inspect: openshell policy get ${SANDBOX_NAME} --full" >&2
    exit 1
fi
if printf '%s' "$EFFECTIVE_POLICY" | grep -q 'ledger_router\|ledger-router'; then
    echo "${YELLOW}warning: the effective policy still mentions the middleware${RESET}" >&2
    echo "  Inspect: openshell policy get ${SANDBOX_NAME} --full" >&2
    exit 1
fi

echo "  ${GREEN}ok${RESET}  no network_middlewares entry in the effective policy"
echo "  ${GREEN}${BOLD}ROUTER OFF${RESET}  ${SANDBOX_NAME}"
echo
echo "${BOLD}Next${RESET}"
echo "  This is the OFF condition for the leak test."
echo "  To re-attach: bash sandbox/router_on.sh"
