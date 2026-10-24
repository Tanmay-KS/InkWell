#!/usr/bin/env bash
# Register the Ledger privacy router with the OpenShell gateway.
#
# The gateway calls every registered middleware at startup and REFUSES TO
# START if one is unavailable. So this script, in order:
#
#   1. proves the service answers Describe and redacts a synthetic request
#   2. generates a gateway TOML with the middleware block enabled
#   3. validates it with `openshell-gateway config preflight`
#   4. backs up the installed config, installs the new one, restarts
#   5. waits for the gateway; on failure, restores the backup and restarts
#
# Usage:
#   bash sandbox/router_register.sh
#   bash sandbox/router_register.sh --dry-run
#   bash sandbox/router_register.sh --no-restart
#
# The service must be running BEFORE this script. Start it with:
#   bash sandbox/middleware/ledger-router/run.sh
#
# The gateway restart command can be overridden for a non-systemd host:
#   LEDGER_GATEWAY_RESTART_CMD='launchctl kickstart -k gui/$(id -u)/openshell-gateway' ...

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=/dev/null
. "${SCRIPT_DIR}/select_python.sh"
PROBE_PYTHON="$ROUTER_PYTHON"

GATEWAY_NAME="${LEDGER_GATEWAY_NAME:-}"
CONFIG="${LEDGER_GATEWAY_CONFIG:-${HOME}/.config/openshell/gateway.toml}"
ROUTER_ENDPOINT="${LEDGER_ROUTER_ENDPOINT:-127.0.0.1:50051}"
RESTART_CMD="${LEDGER_GATEWAY_RESTART_CMD:-systemctl --user restart openshell-gateway}"
DRY_RUN=0
NO_RESTART=0

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

while [ "$#" -gt 0 ]; do
    case "$1" in
        --gateway) GATEWAY_NAME="${2:-}"; shift 2 ;;
        --gateway=*) GATEWAY_NAME="${1#*=}"; shift ;;
        --config) CONFIG="${2:-}"; shift 2 ;;
        --config=*) CONFIG="${1#*=}"; shift ;;
        --endpoint) ROUTER_ENDPOINT="${2:-}"; shift 2 ;;
        --endpoint=*) ROUTER_ENDPOINT="${1#*=}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --no-restart) NO_RESTART=1; shift ;;
        -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

TMP_CONFIG="$(mktemp "${TMPDIR:-/tmp}/ledger-gateway-router-XXXXXX.toml")"
BACKUP=""
cleanup() {
    if [ "$DRY_RUN" -eq 0 ]; then
        rm -f "$TMP_CONFIG"
    fi
}
trap cleanup EXIT

echo "${BOLD}1. The middleware service${RESET}"
if ! "$PROBE_PYTHON" "${SCRIPT_DIR}/middleware/ledger-router/probe.py" \
    --endpoint "$ROUTER_ENDPOINT" --self-test; then
    die "the router service is not healthy at ${ROUTER_ENDPOINT} — register nothing until it is"
fi
echo

echo "${BOLD}2. Generate and validate the gateway config${RESET}"
SOURCE="$CONFIG"
[ -f "$SOURCE" ] || SOURCE="${SCRIPT_DIR}/gateway.toml"
"$PROBE_PYTHON" "${SCRIPT_DIR}/gateway_config.py" --enable \
    --input "$SOURCE" --output "$TMP_CONFIG" >/dev/null || die "could not generate the config"
if ! timeout 30s openshell-gateway config preflight --path "$TMP_CONFIG"; then
    die "the generated gateway config did not pass preflight"
fi
echo "  ${GREEN}ok${RESET}  preflight passed: $(basename "$TMP_CONFIG")"
echo

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

echo "${BOLD}3. Install the config${RESET}"
mkdir -p "$(dirname "$CONFIG")"
install -m 600 "$TMP_CONFIG" "$CONFIG"
echo "  ${GREEN}ok${RESET}  ${CONFIG}"
echo

if [ "$NO_RESTART" -eq 1 ]; then
    echo "${YELLOW}--no-restart: install only.${RESET}"
    echo "  Restart the gateway before applying the policy:"
    echo "    ${RESTART_CMD}"
    echo "  Then: bash sandbox/router_on.sh"
    exit 0
fi

echo "${BOLD}4. Restart the gateway${RESET}"
if ! bash -c "$RESTART_CMD"; then
    echo "${YELLOW}could not run: ${RESTART_CMD}${RESET}"
    echo "  The config is installed but the gateway was not restarted."
    echo "  Restart it manually, then: bash sandbox/router_on.sh"
    exit 2
fi

echo "  waiting for the gateway to answer..."
UP=0
for _ in $(seq 1 30); do
    if openshell status >/dev/null 2>&1; then
        UP=1
        break
    fi
    sleep 1
done

if [ "$UP" -eq 1 ]; then
    echo "  ${GREEN}ok${RESET}  gateway is up with the middleware registered"
    echo
    echo "${GREEN}${BOLD}REGISTERED${RESET}  ledger-router at ${ROUTER_ENDPOINT}"
    echo
    echo "${BOLD}Next${RESET}"
    echo "  bash sandbox/router_on.sh"
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
echo "  Check: openshell status ; openshell logs --source gateway"
exit 1
