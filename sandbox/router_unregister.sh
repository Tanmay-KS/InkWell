#!/usr/bin/env bash
# Remove the Ledger privacy router from the OpenShell gateway config.
#
# Do this AFTER detaching the policy (`sandbox/router_off.sh`). A live
# policy that names a middleware which is no longer registered is a broken
# policy, and the gateway's fail-closed default will block the sandbox.
#
#   1. refuse if the sandbox policy still names the router (unless --force)
#   2. generate a gateway TOML with the middleware block commented out
#   3. preflight it, back up the installed file, install, restart
#   4. on failure, restore the backup and restart
#
# Usage:
#   bash sandbox/router_unregister.sh
#   bash sandbox/router_unregister.sh --dry-run
#   bash sandbox/router_unregister.sh --force --no-restart

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SANDBOX_NAME="${LEDGER_SANDBOX_NAME:-ledger-hermes}"
GATEWAY_NAME="${LEDGER_GATEWAY_NAME:-}"
CONFIG="${LEDGER_GATEWAY_CONFIG:-${HOME}/.config/openshell/gateway.toml}"
RESTART_CMD="${LEDGER_GATEWAY_RESTART_CMD:-systemctl --user restart openshell-gateway}"
DRY_RUN=0
NO_RESTART=0
FORCE=0
PYTHON="${LEDGER_ROUTER_PYTHON:-python3}"

BOLD=$'\033[1m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'

while [ "$#" -gt 0 ]; do
    case "$1" in
        --gateway) GATEWAY_NAME="${2:-}"; shift 2 ;;
        --gateway=*) GATEWAY_NAME="${1#*=}"; shift ;;
        --sandbox) SANDBOX_NAME="${2:-}"; shift 2 ;;
        --sandbox=*) SANDBOX_NAME="${1#*=}"; shift ;;
        --config) CONFIG="${2:-}"; shift 2 ;;
        --config=*) CONFIG="${1#*=}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --no-restart) NO_RESTART=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
command -v openshell-gateway >/dev/null 2>&1 || die "openshell-gateway is not on PATH"

if [ "$FORCE" -eq 0 ]; then
    # A failed read is not evidence that the policy is clean. Removing the
    # registration while the policy still names it would leave the sandbox
    # with a policy the gateway refuses.
    if ! POLICY_TEXT="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null)"; then
        die "cannot read the policy for ${SANDBOX_NAME}; refusing to unregister without --force"
    fi
    if printf '%s' "$POLICY_TEXT" | grep -q 'ledger_router\|ledger-router'; then
        echo "${YELLOW}The sandbox policy still names the router.${RESET}" >&2
        echo "  Detach it first: bash sandbox/router_off.sh" >&2
        echo "  Then re-run this script. (Use --force only if the sandbox is gone.)" >&2
        exit 1
    fi
fi

TMP_CONFIG="$(mktemp "${TMPDIR:-/tmp}/ledger-gateway-base-XXXXXX.toml")"
BACKUP=""
cleanup() {
    if [ "$DRY_RUN" -eq 0 ]; then
        rm -f "$TMP_CONFIG"
    fi
}
trap cleanup EXIT

SOURCE="$CONFIG"
[ -f "$SOURCE" ] || SOURCE="${SCRIPT_DIR}/gateway.toml"
"$PYTHON" "${SCRIPT_DIR}/gateway_config.py" --disable \
    --input "$SOURCE" --output "$TMP_CONFIG" >/dev/null || die "could not generate the config"
if ! timeout 30s openshell-gateway config preflight --path "$TMP_CONFIG"; then
    die "the generated gateway config did not pass preflight"
fi
echo "  ${GREEN}ok${RESET}  preflight passed"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "${YELLOW}dry run: nothing installed and nothing restarted.${RESET}"
    echo "  generated config: ${TMP_CONFIG}"
    trap - EXIT
    exit 0
fi

if [ -f "$CONFIG" ]; then
    BACKUP="${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$CONFIG" "$BACKUP"
    echo "  backup: ${BACKUP}"
fi

mkdir -p "$(dirname "$CONFIG")"
install -m 600 "$TMP_CONFIG" "$CONFIG"
echo "  ${GREEN}ok${RESET}  ${CONFIG}"

if [ "$NO_RESTART" -eq 1 ]; then
    echo "${YELLOW}--no-restart: install only.${RESET}"
    echo "  Restart the gateway: ${RESTART_CMD}"
    exit 0
fi

if ! bash -c "$RESTART_CMD"; then
    echo "${YELLOW}could not run: ${RESTART_CMD}${RESET}"
    echo "  The config is installed but the gateway was not restarted."
    echo "  Restart it manually."
    exit 2
fi

UP=0
for _ in $(seq 1 30); do
    if openshell status >/dev/null 2>&1; then
        UP=1
        break
    fi
    sleep 1
done

if [ "$UP" -eq 1 ]; then
    echo "  ${GREEN}ok${RESET}  gateway is up without the middleware registration"
    echo
    echo "${GREEN}${BOLD}UNREGISTERED${RESET}  ledger-router"
    echo
    echo "${BOLD}Next${RESET}"
    echo "  The base policy is the router OFF state. To re-register:"
    echo "    bash sandbox/router_register.sh && bash sandbox/router_on.sh"
    exit 0
fi

echo "  ${RED}the gateway did not answer after the restart${RESET}"
echo "  restoring the previous config..."
if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
    install -m 600 "$BACKUP" "$CONFIG"
else
    install -m 600 "${SCRIPT_DIR}/gateway.toml" "$CONFIG"
fi
bash -c "$RESTART_CMD" >/dev/null 2>&1 || true
echo "  ${YELLOW}restored. The gateway may need a manual restart.${RESET}"
exit 1
