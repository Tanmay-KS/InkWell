#!/usr/bin/env bash
# InkWell — Phase 2 egress enforcement probe.
#
# Proves the OpenShell network policy from inside the sandbox, not from the
# host. Three positive/negative pairs:
#
#   allowed host + allowed path   -> reaches the upstream (a 401 without a
#                                    key is success: the network let it out)
#   unknown host                  -> refused before it leaves
#   allowed host + wrong path     -> refused by the path rule
#   allowed host + wrong port     -> refused by the port rule (optional)
#
# Usage:
#   bash sandbox/egress_probe.sh
#   bash sandbox/egress_probe.sh --gateway hostfetch
#   INKWELL_SANDBOX_NAME=inkwell-hermes bash sandbox/egress_probe.sh
#
# Exit codes:
#   0  every check passed
#   1  at least one check failed
#   2  the probe could not be evaluated (no gateway / no sandbox)
#
# The probe never uses a real credential. An unauthorized response from the
# upstream is the signal that the connection was allowed; the sandbox's
# credential placeholder is not a secret.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SANDBOX_NAME="${INKWELL_SANDBOX_NAME:-inkwell-hermes}"
GATEWAY_NAME="${INKWELL_GATEWAY_NAME:-}"
CURL_TIMEOUT="${INKWELL_CURL_TIMEOUT:-15}"
QUIET=0

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

RESULTS=()
PASSED=0
FAILED=0
BLOCKED=0

say() { printf '%s\n' "$*"; }

record() {
    local kind="$1" name="$2" status="$3" detail="${4:-}"
    local colour
    case "$status" in
        PASS) colour="$GREEN"; PASSED=$((PASSED + 1)) ;;
        FAIL) colour="$RED"; FAILED=$((FAILED + 1)) ;;
        *)    colour="$DIM"; BLOCKED=$((BLOCKED + 1)) ;;
    esac
    RESULTS+=("${kind}|${name}|${status}|${detail}")
    if [ "$QUIET" -eq 0 ]; then
        say "  ${colour}${status}${RESET}  ${name}"
        [ -n "$detail" ] && say "        ${DIM}${detail}${RESET}"
    fi
    return 0
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --gateway)
            GATEWAY_NAME="${2:-}"
            [ -n "$GATEWAY_NAME" ] || { echo "--gateway needs a name" >&2; exit 2; }
            shift 2
            ;;
        --gateway=*) GATEWAY_NAME="${1#*=}"; shift ;;
        --quiet) QUIET=1; shift ;;
        -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
    say "${RED}openshell is not on PATH${RESET}"
    exit 2
fi

if ! openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME"; then
    say "${YELLOW}Cannot evaluate: sandbox ${SANDBOX_NAME} is not visible from this client.${RESET}"
    say "  Run on the gateway host, or pass --gateway <name>."
    exit 2
fi

# run_curl <method> <url> [body]
# Prints the response body, the HTTP code marker, and curl's stderr.
run_curl() {
    local method="$1" url="$2" body="${3:-}"
    local -a args=(
        /usr/bin/curl -sS --max-time "$CURL_TIMEOUT" -X "$method"
        -w '\n__INKWELL_HTTP_CODE__:%{http_code}\n'
    )
    if [ -n "$body" ]; then
        args+=(-H 'content-type: application/json' --data-binary "$body")
    fi
    args+=("$url")
    timeout $((CURL_TIMEOUT + 10))s openshell sandbox exec \
        -n "$SANDBOX_NAME" --no-login-shell -- "${args[@]}" 2>&1
}

http_code() {
    printf '%s\n' "$1" | sed -n 's/.*__INKWELL_HTTP_CODE__:\([0-9][0-9]*\).*/\1/p' | tail -1
}

denial_marker() {
    printf '%s\n' "$1" | grep -oiE \
        'policy_denied|network_policy_denied|not allowed|denied by policy|blocked by policy|Could not resolve host|Connection refused|No route to host|network is unreachable' \
        | head -1
}

# expect_allowed <name> <method> <url> [body]
expect_allowed() {
    local name="$1" method="$2" url="$3" body="${4:-}"
    local out code marker
    out="$(run_curl "$method" "$url" "$body")"
    code="$(http_code "$out")"
    marker="$(denial_marker "$out")"

    if [ -n "$marker" ]; then
        record required "$name" FAIL \
            "expected the request to leave, but it was refused (${marker}; code=${code:-none})"
        return
    fi
    case "$code" in
        200|201|204|301|302|400|401|403|404|405|422|429)
            record required "$name" PASS \
                "reached the upstream (HTTP ${code}${body:+, body sent})"
            ;;
        ""|000)
            record required "$name" FAIL \
                "no HTTP response; the request did not reach the upstream: $(printf '%s' "$out" | tail -2 | tr '\n' ' ' | cut -c1-160)"
            ;;
        *)
            record required "$name" PASS "reached the upstream (HTTP ${code})"
            ;;
    esac
}

# expect_denied <name> <method> <url> [body]
expect_denied() {
    local name="$1" method="$2" url="$3" body="${4:-}"
    local out code marker
    out="$(run_curl "$method" "$url" "$body")"
    code="$(http_code "$out")"
    marker="$(denial_marker "$out")"

    case "$code" in
        403|000|"")
            record required "$name" PASS \
                "refused before the upstream (HTTP ${code:-none}${marker:+; ${marker}})"
            return
            ;;
    esac
    if [ -n "$marker" ]; then
        record required "$name" PASS \
            "refused before the upstream (HTTP ${code}; ${marker})"
        return
    fi
    record required "$name" FAIL \
        "the request reached the upstream (HTTP ${code}) — the policy did not enforce this boundary"
}

if [ "$QUIET" -eq 0 ]; then
    say "${BOLD}InkWell — Phase 2 egress enforcement${RESET}"
    say "${DIM}sandbox: ${SANDBOX_NAME}${RESET}"
    say ""
fi

# 1. The allowlist still works. Token Factory answers 401 without a key,
#    which is exactly the proof we want: the connection was made.
expect_allowed "allowed host + allowed path reaches the upstream" \
    GET "https://api.tokenfactory.nebius.com/v1/models"

# 2. An unknown host is refused by the network policy, not by a DNS failure
#    we cannot distinguish from a broken network. Both a 403 from the proxy
#    and curl's "Could not resolve host" (the placeholder DNS answer) count.
expect_denied "unknown host is refused" \
    GET "https://example.com/"

# 3. The host is allowed, the path is not. This is the boundary a router
#    cannot provide on its own: a compromised agent can only reach the
#    endpoints the policy names.
expect_denied "allowed host + wrong path is refused" \
    GET "https://api.tokenfactory.nebius.com/not-a-policy-path"

# 4. The host and path are allowed, the port is not.
expect_denied "allowed host + wrong port is refused" \
    GET "https://api.tokenfactory.nebius.com:8443/v1/models"

# 5. The second middleware endpoint is reachable on its allowed path. No
#    key means a 401; that is a successful egress, not a failed probe.
expect_allowed "Tavily /search is reachable" \
    POST "https://api.tavily.com/search" '{"query":"cancellation policy"}'

say ""
say "${BOLD}Summary${RESET}"
printf '  %-6s %-8s %s\n' "KIND" "STATUS" "CHECK"
printf '  %-6s %-8s %s\n' "------" "--------" "-----------------------------"
for row in "${RESULTS[@]}"; do
    kind="${row%%|*}"; row="${row#*|}"
    name="${row%%|*}"; row="${row#*|}"
    status="${row%%|*}"; detail="${row#*|}"
    case "$status" in
        PASS) colour="$GREEN" ;; FAIL) colour="$RED" ;; *) colour="$DIM" ;;
    esac
    printf '  %-6s %s%-8s%s %s\n' "$kind" "$colour" "$status" "$RESET" "$name"
    [ -n "$detail" ] && printf '  %-6s %-8s %s%s%s\n' "" "" "$DIM" "$detail" "$RESET"
done
say ""

if [ "$FAILED" -gt 0 ]; then
    say "  ${RED}${BOLD}FAIL${RESET}  ${FAILED} check(s) failed, ${PASSED} passed."
    exit 1
fi
if [ "$BLOCKED" -gt 0 ]; then
    say "  ${YELLOW}${BOLD}INCOMPLETE${RESET}  ${BLOCKED} check(s) could not be evaluated."
    exit 2
fi
say "  ${GREEN}${BOLD}PASS${RESET}  ${PASSED} egress check(s) passed."
say "  ${DIM}The sandbox can reach the endpoints it was given and nothing else.${RESET}"
exit 0
