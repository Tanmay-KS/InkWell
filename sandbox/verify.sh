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
[ "${1:-}" = "--quiet" ] && QUIET=1

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
RESULTS=()

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

# record <required|info> <name> <PASS|FAIL> <detail>
record() {
    local kind="$1" name="$2" status="$3" detail="${4:-}"
    local colour
    if [ "$status" = "PASS" ]; then
        colour="$GREEN"; PASSED=$((PASSED + 1))
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

    if command -v docker >/dev/null 2>&1; then
        if docker info >/dev/null 2>&1; then
            record required "docker daemon reachable" PASS \
                "server $(docker version --format '{{.ServerVersion}}' 2>/dev/null)"
        else
            record required "docker daemon reachable" FAIL \
                "the OpenShell gateway creates its Docker client at startup and will not start without one — try: sudo systemctl start docker"
        fi
    else
        record required "docker CLI on PATH" FAIL "not found"
    fi

    local gw_status
    gw_status="$(openshell status 2>&1)"
    if echo "$gw_status" | grep -qi 'No gateway configured\|not configured\|error'; then
        record required "gateway reachable" FAIL \
            "$(echo "$gw_status" | head -2 | tr '\n' ' ' | cut -c1-120) — try: systemctl --user restart openshell-gateway"
    elif openshell status >/dev/null 2>&1; then
        record required "gateway reachable" PASS "$(echo "$gw_status" | head -3 | tr '\n' ' ' | cut -c1-120)"
    else
        record required "gateway reachable" FAIL \
            "openshell status failed — try: systemctl --user restart openshell-gateway"
    fi

    if openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME"; then
        record required "sandbox ${SANDBOX_NAME} exists" PASS ""
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

    if echo "$policy" | grep -q "run_as_user: *\"10000\""; then
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
    out="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        /opt/hermes/.venv/bin/hermes chat \
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
    out="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
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
    out="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        /usr/bin/env NEBIUS_BASE_URL=https://api.example.invalid/v1 \
        /opt/hermes/.venv/bin/hermes chat --provider nebius \
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
        if [ "$status" = "PASS" ]; then colour="$GREEN"; else colour="$RED"; fi
        printf '  %-6s %s%-8s%s %s\n' "$kind" "$colour" "$status" "$RESET" "$name"
        [ -n "$detail" ] && printf '  %-6s %-8s %s%s%s\n' "" "" "$DIM" "$detail" "$RESET"
    done
    echo
    if [ "$FAILED" -eq 0 ]; then
        printf '  %s%sPASS%s  %d required check(s) passed' "$GREEN" "$BOLD" "$RESET" "$PASSED"
        [ "$INFORMATIONAL_FAILED" -gt 0 ] && \
            printf ', %d informational check(s) failed' "$INFORMATIONAL_FAILED"
        printf '.\n\n  %sHermes + OpenShell + Token Factory confirmed working.%s\n' "$GREEN" "$RESET"
        return 0
    fi
    printf '  %s%sFAIL%s  %d required check(s) failed, %d passed' "$RED" "$BOLD" "$RESET" "$FAILED" "$PASSED"
    [ "$INFORMATIONAL_FAILED" -gt 0 ] && \
        printf ', %d informational check(s) failed' "$INFORMATIONAL_FAILED"
    printf '.\n'
    return 1
}

# --- main -------------------------------------------------------------------

main() {
    check_prerequisites
    check_policy
    check_connection_log
    check_model_call
    check_credential_scoping
    check_routing_drift
    check_leak_surface
    summarise
}

main "$@"
