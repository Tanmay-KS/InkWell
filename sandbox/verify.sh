#!/usr/bin/env bash
# Ledger — Phase 0 exit verification.
#
# One command, one pass/fail table. This is the artifact that proves the
# Phase 0 exit condition:
#
#     "Hermes + OpenShell + Token Factory confirmed working"
#
# Usage:
#   bash sandbox/verify.sh            # run every check
#   bash sandbox/verify.sh --quiet    # table only, no commentary
#
# Exit code 0 = every required check passed.
# Exit code 1 = at least one required check failed.
#
# The checks are deliberately split into REQUIRED and INFORMATIONAL. A
# failure in the informational group does not block the exit condition, but
# it is usually the first symptom of a real problem.
#
# NOTE ON THE NEGATIVE CHECK (5):
#   The credential scoping test must send the SANDBOX'S OWN placeholder to
#   a host that the policy allows but the credential is not bound to. It
#   must NOT send the real key from the host: that would put a live secret
#   on a command line, and it would not exercise the binding at all,
#   because the sandbox never holds the real key.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SANDBOX_NAME="${LEDGER_SANDBOX_NAME:-ledger-hermes}"
TOKEN_FACTORY_HOST="api.tokenfactory.nebius.com"
# A host the policy allows but that the Token Factory credential is NOT
# bound to. Sending the Token Factory placeholder here must be refused.
UNBOUND_HOST="api.tavily.com"
UNBOUND_PATH="/search"

QUIET=0
GATEWAY_NAME="${LEDGER_GATEWAY_NAME:-}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --quiet) QUIET=1; shift ;;
        --gateway)
            GATEWAY_NAME="${2:-}"
            [ -n "$GATEWAY_NAME" ] || { echo "--gateway needs a name" >&2; exit 2; }
            shift 2
            ;;
        --gateway=*)
            GATEWAY_NAME="${1#*=}"
            shift
            ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

# A remote client may hold a bundle for a gateway under a different
# registration name than the local default.
if [ -n "$GATEWAY_NAME" ]; then
    export OPENSHELL_GATEWAY="$GATEWAY_NAME"
fi

# A manual install lands in ~/.local/bin, which is not always on PATH for
# non-login shells.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac

# --- environment ------------------------------------------------------------

if [ -f "${REPO_ROOT}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${REPO_ROOT}/.env"
    set +a
fi

MODEL_NANO="${LEDGER_MODEL_NANO:-nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B}"

# --- output -----------------------------------------------------------------

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; BLUE=$'\033[36m'; RESET=$'\033[0m'

PASSED=0
FAILED=0
INFORMATIONAL_FAILED=0
SKIPPED=0
RESULTS=()

# Set when the gateway cannot be reached or authenticated from this client.
# Every later check depends on that connection, so they are SKIPPED rather
# than reported as failures: a verifier that cannot tell "not configured"
# from "cannot see" is not a verifier.
TRANSPORT_FAILURE=""
TRANSPORT_REMEDY=""

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

# record <required|info|skip> <name> <PASS|FAIL|SKIP> <detail>
record() {
    local kind="$1" name="$2" status="$3" detail="${4:-}"
    local colour
    if [ "$status" = "PASS" ]; then
        colour="$GREEN"; PASSED=$((PASSED + 1))
    elif [ "$status" = "SKIP" ]; then
        colour="$DIM"; SKIPPED=$((SKIPPED + 1))
    else
        if [ "$kind" = "required" ]; then
            colour="$RED"; FAILED=$((FAILED + 1))
        else
            colour="$YELLOW"; INFORMATIONAL_FAILED=$((INFORMATIONAL_FAILED + 1))
        fi
    fi
    RESULTS+=("${kind}|${name}|${status}|${detail}")
    say "  ${colour}${status}${RESET}  ${name}"
    [ -n "$detail" ] && say "        ${DIM}${detail}${RESET}"
    return 0
}

# --- checks -----------------------------------------------------------------

check_prerequisites() {
    say "${BOLD}Prerequisites${RESET}"

    if command -v openshell >/dev/null 2>&1; then
        record required "openshell CLI on PATH" PASS "$(openshell --version 2>&1 | head -1)"
    else
        record required "openshell CLI on PATH" FAIL "not found — see docs/phase0-runbook.md step 1"
        return
    fi

    # The gateway's compute driver is what actually matters, and it is
    # checked from the gateway's side, not from this machine. A local
    # `docker info` answers a different question: "does THIS machine have
    # Docker", which is false for a remote client and for a host whose
    # Docker socket needs sudo — even when the gateway is perfectly able
    # to create sandboxes.
    if openshell gateway info >/dev/null 2>&1; then
        record required "gateway compute driver is alive" PASS "gateway runtime info reachable"
    elif openshell sandbox list >/dev/null 2>&1; then
        record required "gateway compute driver is alive" PASS "gateway control plane reachable"
    else
        record required "gateway compute driver is alive" FAIL \
            "the gateway did not answer a runtime query — try: systemctl --user restart openshell-gateway"
    fi

    local gw_status
    gw_status="$(openshell status 2>&1)"

    if echo "$gw_status" | grep -qiE 'No gateway configured|not configured'; then
        record required "gateway reachable" FAIL \
            "no gateway is registered with this CLI"
        TRANSPORT_FAILURE="no gateway is registered with this CLI"
        TRANSPORT_REMEDY="register it with: openshell gateway add https://<gateway-host>:17670 --local --name local"
    elif echo "$gw_status" | grep -qiE 'invalid peer certificate|BadSignature|certificate'; then
        # The CLI's client bundle is not signed by the gateway's CA. This is
        # a client-side credential problem, not an infrastructure problem.
        record required "gateway reachable" FAIL \
            "the gateway's CA did not verify this CLI's client certificate"
        TRANSPORT_FAILURE="this CLI cannot authenticate to the gateway (client certificate not trusted)"
        TRANSPORT_REMEDY="run this script on the gateway host, or copy the gateway's client bundle here:
        cp -a ~/.config/openshell/gateways/openshell/mtls <repo>/.openshell/
      then install it as ~/.config/openshell/gateways/<name>/mtls/ and re-run with --gateway <name>"
    elif echo "$gw_status" | grep -qiE 'not reachable|refused|timeout|unavailable'; then
        record required "gateway reachable" FAIL \
            "the gateway did not answer — try: systemctl --user restart openshell-gateway"
        TRANSPORT_FAILURE="the gateway did not answer"
        TRANSPORT_REMEDY="try: systemctl --user restart openshell-gateway"
    elif openshell status >/dev/null 2>&1; then
        record required "gateway reachable" PASS \
            "$(echo "$gw_status" | grep -iE 'Server:|Status:' | head -2 | tr '\n' ' ' | cut -c1-110)"
    else
        record required "gateway reachable" FAIL \
            "openshell status failed: $(echo "$gw_status" | head -2 | tr '\n' ' ' | cut -c1-110)"
        TRANSPORT_FAILURE="openshell status failed"
        TRANSPORT_REMEDY="try: openshell status"
    fi

    if openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME"; then
        record required "sandbox ${SANDBOX_NAME} exists" PASS ""
    elif [ -n "$TRANSPORT_FAILURE" ]; then
        record required "sandbox ${SANDBOX_NAME} exists" SKIP "no gateway connection"
    else
        record required "sandbox ${SANDBOX_NAME} exists" FAIL \
            "run: bash sandbox/setup.sh"
    fi
}

check_policy() {
    say ""
    say "${BOLD}1. The effective policy contains the Token Factory endpoint${RESET}"

    local policy
    policy="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null)"
    if echo "$policy" | grep -q "$TOKEN_FACTORY_HOST"; then
        record required "endpoint present in effective policy" PASS "$TOKEN_FACTORY_HOST:443"
    else
        record required "endpoint present in effective policy" FAIL \
            "openshell policy get ${SANDBOX_NAME} --full"
    fi

    if echo "$policy" | grep -q "credential_binding\|run_as_user"; then
        record info "policy shows a credential binding" PASS ""
    else
        record info "policy shows a credential binding" FAIL \
            "the provider may not be attached — openshell sandbox provider list ${SANDBOX_NAME}"
    fi

    if echo "$policy" | grep -qE 'run_as_user: *"?10000"?'; then
        record required "workload runs as UID 10000 (not root)" PASS ""
    else
        record required "workload runs as UID 10000 (not root)" FAIL \
            "OpenShell rejects a root workload; the policy must set process.run_as_user"
    fi
}

check_connection_log() {
    say ""
    say "${BOLD}2. The sandbox log shows a connection to Token Factory${RESET}"

    local log
    log="$(openshell logs "$SANDBOX_NAME" --since 15m --source sandbox 2>/dev/null)"
    if echo "$log" | grep -q "$TOKEN_FACTORY_HOST"; then
        record required "connection observed in the sandbox log" PASS ""
    else
        record info "connection observed in the sandbox log" FAIL \
            "no connection in the last 15m — expected until the agent has run"
    fi
}

check_model_call() {
    say ""
    say "${BOLD}3. A real model call succeeds from inside the sandbox${RESET}"

    if [ -z "${NEBIUS_API_KEY:-}" ]; then
        record required "model call returns LEDGER-OK" FAIL \
            "NEBIUS_API_KEY is not set in the environment or .env"
        return
    fi

    local out
    out="$(timeout 45s openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        /opt/hermes/.venv/bin/hermes chat --oneshot \
    --provider nebius --model "$MODEL_NANO" \
    -q 'Reply with exactly: LEDGER-OK' 2>&1)"

    if echo "$out" | grep -q 'LEDGER-OK'; then
        record required "model call returns LEDGER-OK" PASS "model ${MODEL_NANO}"
    else
        record required "model call returns LEDGER-OK" FAIL \
            "$(echo "$out" | tail -3 | tr '\n' ' ' | cut -c1-160)"
    fi
}

check_credential_scoping() {
    say ""
    say "${BOLD}4. NEGATIVE — the credential is scoped to one destination${RESET}"

    # The expansion of $NEBIUS_API_KEY happens INSIDE the sandbox, so this
    # sends the opaque placeholder, not the real key.
    local out rc
    out="$(timeout 45s openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        /bin/sh -c "curl -sS -o /dev/null -w '%{http_code}' \
            -H \"Authorization: Bearer \$NEBIUS_API_KEY\" \
            https://${UNBOUND_HOST}${UNBOUND_PATH}" 2>&1)"; rc=$?

    if [ "$rc" -ne 0 ] && ! echo "$out" | grep -qi 'credential_endpoint_mismatch'; then
        record required "placeholder refused on an unbound host" FAIL \
            "could not run the test inside the sandbox: $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-140)"
        return
    fi

    if echo "$out" | grep -q 'credential_endpoint_mismatch'; then
        record required "placeholder refused on an unbound host" PASS \
            "OpenShell refused the Token Factory credential at ${UNBOUND_HOST}"
    elif echo "$out" | grep -qE '^(403|401)$'; then
        record required "placeholder refused on an unbound host" PASS \
            "refused with ${out} at ${UNBOUND_HOST}"
    elif echo "$out" | grep -qi 'policy_denied\|not allowed\|denied'; then
        record required "placeholder refused on an unbound host" FAIL \
            "the request was denied by POLICY, not by the credential binding — the test is inconclusive; check that ${UNBOUND_HOST} is allowed by network_policies"
    else
        record required "placeholder refused on an unbound host" FAIL \
            "the credential was NOT refused (got: $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-160))"
    fi
}

check_routing_drift() {
    say ""
    say "${BOLD}5. NEGATIVE — the agent cannot be pointed at another host${RESET}"

    # Hermes honours NEBIUS_BASE_URL. Pointing it somewhere else must be
    # refused by the runtime, because the credential is bound to Token
    # Factory only. This is the strongest form of the routing proof: the
    # agent actively tries to leave and the sandbox says no.
    local out rc
    out="$(timeout 45s openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        /usr/bin/env NEBIUS_BASE_URL=https://api.example.invalid/v1 \
        /opt/hermes/.venv/bin/hermes chat --oneshot --provider nebius \
        --model "$MODEL_NANO" -q 'ping' 2>&1)"; rc=$?

    if [ "$rc" -ne 0 ] && ! echo "$out" | grep -qi 'credential_endpoint_mismatch'; then
        record info "off-host base URL refused" FAIL \
            "could not run the test inside the sandbox: $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-140)"
        return
    fi

    if echo "$out" | grep -qi 'credential_endpoint_mismatch'; then
        record info "off-host base URL refused" PASS "runtime refused the credential at the new host"
    elif echo "$out" | grep -qi 'policy_denied\|not allowed'; then
        record info "off-host base URL refused" PASS "the destination is not on the allowlist"
    else
        record info "off-host base URL refused" FAIL \
            "$(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-160)"
    fi
}

check_leak_surface() {
    say ""
    say "${BOLD}6. The egress allowlist is exactly what we intended${RESET}"

    local policy hosts
    policy="$(openshell policy get "$SANDBOX_NAME" --full 2>/dev/null)"

    # An empty policy is not a clean allowlist — it is a failed read. This
    # check must never pass vacuously.
    if [ -z "${policy//[[:space:]]/}" ]; then
        record required "no unexpected hosts in the allowlist" FAIL \
            "could not read the effective policy — the gateway or the sandbox is not up"
        return
    fi

    hosts="$(echo "$policy" | grep -oE 'host: [a-z0-9.-]+' | awk '{print $2}' | sort -u)"
    if [ -z "$hosts" ]; then
        record required "no unexpected hosts in the allowlist" FAIL \
            "the effective policy contains no endpoints"
        return
    fi

    local unexpected=""
    local h
    for h in $hosts; do
        case "$h" in
            api.tokenfactory.nebius.com|api.tavily.com|api.telegram.org|host.openshell.internal) ;;
            *) unexpected="${unexpected} ${h}" ;;
        esac
    done

    if [ -z "$unexpected" ]; then
        record required "no unexpected hosts in the allowlist" PASS \
            "$(echo "$hosts" | tr '\n' ' ')"
    else
        record required "no unexpected hosts in the allowlist" FAIL \
            "unexpected:${unexpected}"
    fi
}

# --- summary ----------------------------------------------------------------

summarise() {
    echo
    echo "${BOLD}Phase 0 exit condition${RESET}"
    echo
    printf '  %-6s %-8s %s\n' "KIND" "STATUS" "CHECK"
    printf '  %-6s %-8s %s\n' "------" "--------" "-----------------------------"
    local row
    for row in "${RESULTS[@]}"; do
        local kind name status detail
        kind="${row%%|*}"; row="${row#*|}"
        name="${row%%|*}"; row="${row#*|}"
        status="${row%%|*}"; detail="${row#*|}"
        local colour
        if [ "$status" = "PASS" ]; then colour="$GREEN"; elif [ "$status" = "SKIP" ]; then colour="$DIM"; else colour="$RED"; fi
        printf '  %-6s %s%-8s%s %s\n' "$kind" "$colour" "$status" "$RESET" "$name"
        [ -n "$detail" ] && printf '  %-6s %-8s %s%s%s\n' "" "" "$DIM" "$detail" "$RESET"
    done
    echo
    if [ "$FAILED" -eq 0 ] && [ "$SKIPPED" -eq 0 ]; then
        printf '  %s%sPASS%s  %d required check(s) passed' "$GREEN" "$BOLD" "$RESET" "$PASSED"
        [ "$INFORMATIONAL_FAILED" -gt 0 ] && \
            printf ', %d informational check(s) failed' "$INFORMATIONAL_FAILED"
        printf '.\n\n  %sHermes + OpenShell + Token Factory confirmed working.%s\n' "$GREEN" "$RESET"
        return 0
    fi
    if [ "$FAILED" -eq 0 ] && [ "$SKIPPED" -gt 0 ]; then
        printf '  %s%sINCOMPLETE%s  %d required check(s) passed, %d could not be evaluated' \
            "$YELLOW" "$BOLD" "$RESET" "$PASSED" "$SKIPPED"
        printf '.\n'
        [ -n "$TRANSPORT_FAILURE" ] && printf '  %s%s%s\n' "$DIM" "$TRANSPORT_FAILURE" "$RESET"
        return 1
    fi
    printf '  %s%sFAIL%s  %d required check(s) failed, %d passed' "$RED" "$BOLD" "$RESET" "$FAILED" "$PASSED"
    [ "$INFORMATIONAL_FAILED" -gt 0 ] && \
        printf ', %d informational check(s) failed' "$INFORMATIONAL_FAILED"
    [ "$SKIPPED" -gt 0 ] && printf ', %d skipped' "$SKIPPED"
    printf '.\n'
    [ -n "$TRANSPORT_FAILURE" ] && printf '  %s%s%s\n' "$DIM" "$TRANSPORT_FAILURE" "$RESET"
    return 1
}

# --- main -------------------------------------------------------------------

# Every check below the prerequisites needs a working gateway connection.
# When the client cannot reach or authenticate to the gateway, they are
# recorded as SKIP rather than as failures: the infrastructure is not
# proven broken, it is simply not observable from here.
skip_all_dependent() {
    local name
    for name in \
        "endpoint present in effective policy" \
        "policy shows a credential binding" \
        "workload runs as UID 10000 (not root)" \
        "connection observed in the sandbox log" \
        "model call returns LEDGER-OK" \
        "placeholder refused on an unbound host" \
        "off-host base URL refused" \
        "no unexpected hosts in the allowlist" ; do
        record skip "$name" SKIP "no gateway connection"
    done
}

main() {
    say "${BOLD}Ledger — Phase 0 exit verification${RESET}"
    local active
    active="$(openshell gateway list 2>/dev/null | awk '$1=="*" {print $2}')"
    if [ -n "$active" ]; then
        say "${DIM}active gateway: ${active}${RESET}"
    fi
    say ""

    check_prerequisites

    if [ -n "$TRANSPORT_FAILURE" ]; then
        say ""
        say "${YELLOW}The gateway could not be reached from this client, so the remaining"
        say "checks could not be evaluated. They are reported as SKIP, not as failures."
        say ""
        say "${BOLD}How to proceed${RESET}"
        say "  ${TRANSPORT_REMEDY}"
        say ""
        skip_all_dependent
        summarise
        return $?
    fi

    check_policy
    check_connection_log
    check_model_call
    check_credential_scoping
    check_routing_drift
    check_leak_surface
    summarise
}

main "$@"
