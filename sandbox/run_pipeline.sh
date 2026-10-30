#!/usr/bin/env bash
# InkWell — run the pipeline inside the OpenShell sandbox.
#
# This is the harness for Person A's first Phase 1 task: "wire Hermes/OpenShell
# so B's pipeline runs on the real sandbox from day one, not bolted on later".
# It is the command that makes that checkable.
#
#   bash sandbox/run_pipeline.sh <document.png>      # run the pipeline
#   bash sandbox/run_pipeline.sh <document.png> --check   # plumbing only
#   bash sandbox/run_pipeline.sh --help
#
# What it does:
#
#   1. checks the sandbox is up and the provider is attached
#   2. uploads the document into the sandbox's workspace
#   3. runs the pipeline entry point INSIDE the sandbox
#   4. prints the flagged deadline and the draft action
#   5. shows the egress the run actually used
#
# THE ENTRY-POINT CONTRACT
# ------------------------
# The pipeline is invoked as:
#
#   $INKWELL_PIPELINE_CMD <document-path> [...]
#
# The default entry point is `agent.demo`, which is the Phase 1
# exit-condition runner: it takes document paths as positional arguments,
# reads the rendered scan, and prints the deadline it found, the draft it
# wrote, and the REDACTED document — the version that leaves the sandbox.
# `agent.pipeline` is the library underneath it and has no CLI of its own.
#
# The harness prints whatever the command emits. It does not parse it.
#
# For the Phase 2 leak test the pipeline will need a machine-readable mode
# as well, and the shape that mode should emit is:
#
#   {
#     "document":    "doc-0000.png",
#     "extraction":  {"method": "local", "fields": {...}, "confidence": {...}},
#     "redaction":   {"placeholders": 13, "categories": {"account": 1}},
#     "deadline":    {"kind": "renewal", "title": "...",
#                     "date": "2027-03-06", "evidence": "..."},
#     "action":      {"kind": "cancel", "title": "...",
#                     "body": "...", "recipient": "..."},
#     "egress":      {"hosts": ["api.tokenfactory.nebius.com"], "calls": 3}
#   }
#
# "method" is "local" when the document was read by the in-sandbox
# extractor, and "model" when the fallback read the image directly. The
# fallback is counted in the Phase 2 leak test as its own line item, so it
# has to be visible. The leak-review checklist in
# docs/phase1-runbook.md §4 is written against this shape.
#
# Override the entry point with INKWELL_PIPELINE_CMD.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SANDBOX_NAME="${INKWELL_SANDBOX_NAME:-inkwell-hermes}"
INBOX="/sandbox/inbox"
PIPELINE_CMD="${INKWELL_PIPELINE_CMD:-/opt/hermes/.venv/bin/python /opt/inkwell/inkwell_runner.py}"
PYTHON_BIN="/opt/hermes/.venv/bin/python"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; BLUE=$'\033[36m'; RESET=$'\033[0m'

step() { printf '%s==>%s %s%s%s\n' "$BLUE" "$RESET" "$BOLD" "$*" "$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; }
info() { printf '  %s%s%s\n' "$DIM" "$*" "$RESET"; }
die()  { fail "$*"; exit 1; }

usage() {
    sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

[ "${1:-}" = "--help" ] && usage
[ "${1:-}" = "-h" ] && usage

CHECK_ONLY=0
DOCUMENT=""
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        -*) die "unknown argument: $arg (try --help)" ;;
        *) [ -z "$DOCUMENT" ] && DOCUMENT="$arg" || die "only one document, please" ;;
    esac
done
[ -n "$DOCUMENT" ] || usage
[ -f "$DOCUMENT" ] || die "no such file: ${DOCUMENT}"

# The CLI resolves ~ from the passwd entry, not $HOME, and the OpenShell
# install may land in ~/.local/bin. Put both on PATH.
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# ---------------------------------------------------------------------------

check_sandbox() {
    step "Sandbox"

    if ! command -v openshell >/dev/null 2>&1; then
        die "the openshell CLI is not on PATH — see docs/phase0-runbook.md step 1"
    fi

    if ! openshell status 2>&1 | grep -qiE 'Status: *Connected'; then
        fail "the gateway is not reachable from this client"
        info "run this on the gateway host, or copy the gateway's client bundle"
        info "and register it:  bash sandbox/verify.sh --gateway <name>"
        die "cannot reach the gateway"
    fi
    ok "gateway reachable"

    if ! openshell sandbox list 2>/dev/null | grep -q "$SANDBOX_NAME"; then
        die "no sandbox named ${SANDBOX_NAME} — run: bash sandbox/setup.sh"
    fi
    ok "sandbox ${SANDBOX_NAME} exists"

    if openshell sandbox provider list "$SANDBOX_NAME" 2>/dev/null | grep -q nebius; then
        ok "nebius provider attached"
    else
        warn "the nebius provider does not look attached"
        info "the model call in --check will fail until it is"
    fi
}

upload_document() {
    step "Upload"

    # --no-git-ignore matters: the synthetic corpus is gitignored, and a
    # git-filtered upload silently skips it.
    if ! openshell sandbox upload "$SANDBOX_NAME" "$DOCUMENT" "${INBOX}/" --no-git-ignore \
        >/dev/null 2>&1; then
        die "upload failed — is the sandbox running?"
    fi

    local name
    name="$(basename "$DOCUMENT")"
    ok "uploaded to ${INBOX}/${name}"
    echo "$name"
}

run_plumbing_check() {
    step "Plumbing check (no pipeline required)"

    # A real model call from inside the sandbox. This is the same call
    # verify.sh makes, and it proves the credential placeholder resolves
    # against the Token Factory endpoint from inside the sandbox.
    local out
    out="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        "$PYTHON_BIN" - <<'PY' 2>&1
import json, os, urllib.request

key = os.environ.get("NEBIUS_API_KEY", "")
model = os.environ.get("INKWELL_MODEL_NANO", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
req = urllib.request.Request(
    "https://api.tokenfactory.nebius.com/v1/chat/completions",
    data=json.dumps({"model": model, "max_tokens": 64,
                     "messages": [{"role": "user", "content": "Reply with: OK"}]}).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})

# Only the transport and the decode are failures. Everything below the
# `else` is a call that reached the API, and must never be reported as a
# network problem: a reasoning model can spend the whole budget thinking
# and return `content: null` with finish_reason "length" (F-025, F-040).
try:
    body = json.load(urllib.request.urlopen(req, timeout=60))
except Exception as exc:
    print("MODEL-FAIL", type(exc).__name__, str(exc)[:160])
else:
    if not isinstance(body, dict):
        print("MODEL-FAIL", "BadResponse", "not a JSON object")
    else:
        choices = body.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        finish = choices[0].get("finish_reason") if choices else None
        text = (message.get("content") or "").strip()
        if not choices:
            print("MODEL-FAIL", "NoChoices", "the response carried no completion")
        elif text:
            print("MODEL-OK", text[:40])
        else:
            print("MODEL-OK (no prose, finish_reason=%s)" % (finish,))
PY
)"

    if echo "$out" | grep -q 'MODEL-OK'; then
        ok "model call from inside the sandbox: $(echo "$out" | grep MODEL-OK | head -1)"
    else
        fail "the model call from inside the sandbox failed"
        echo "$out" | sed 's/^/      /'
        info "a placeholder in the sandbox is expected; the real key is substituted"
        info "at the network boundary, only for api.tokenfactory.nebius.com"
        info "a transport error names URLError or HTTPError; anything else is the shape of the reply"
        die "the model call from inside the sandbox did not succeed"
    fi

    # The package collision is silent — the upstream image ships its own
    # `agent` package — so prove which one resolves before the pipeline is
    # allowed to run. The launcher's --where prints the imported file.
    local imports
    imports="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        "$PYTHON_BIN" /opt/inkwell/inkwell_runner.py --where 2>&1)"
    if echo "$imports" | grep -qx '/opt/inkwell/agent/__init__.py'; then
        ok "InkWell packages resolve from /opt/inkwell"
    else
        fail "the sandbox resolves 'agent' from the wrong place"
        echo "$imports" | sed 's/^/      /'
        info "expected /opt/inkwell/agent/__init__.py — the image is stale or"
        info "was built without the InkWell packages; rebuild it"
        die "import check failed"
    fi

    # The write surface: state belongs in /opt/data, scratch in /sandbox.
    if openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        sh -c 'mkdir -p /opt/data/.probe && echo ok > /opt/data/.probe/x && rm -rf /opt/data/.probe' \
        >/dev/null 2>&1; then
        ok "/opt/data is writable — state can persist"
    else
        warn "/opt/data is not writable — the memory sidecar will fail"
    fi
}

run_pipeline() {
    step "Pipeline"

    local name="$1"
    local out
    out="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-login-shell -- \
        /bin/sh -c "cd /sandbox && ${PIPELINE_CMD} ${INBOX}/${name}" 2>&1)"
    local rc=$?

    echo "$out"

    if [ "$rc" -ne 0 ]; then
        echo
        fail "the pipeline exited ${rc}"
        if echo "$out" | grep -q "No module named agent"; then
            info "the pipeline entry point does not exist inside the sandbox."
            info "Expected: ${PIPELINE_CMD}"
            info "agent/ and privacy_router/ belong at /opt/inkwell in the image;"
            info "run: bash sandbox/setup.sh   (rebuilds the image and sandbox)"
        fi
        return "$rc"
    fi
    return 0
}

show_egress() {
    step "Egress the run actually used"

    # The sandbox log is the only honest record of what left.
    local log
    log="$(openshell logs "$SANDBOX_NAME" --since 5m --source sandbox 2>/dev/null)"
    local hosts
    hosts="$(echo "$log" | grep -oE 'api\.[a-z0-9.-]+\.[a-z]+|host\.openshell\.internal' \
             | sort -u | tr '\n' ' ')"

    if [ -n "$hosts" ]; then
        info "hosts seen in the last 5m: ${hosts}"
    else
        info "no outbound connections in the last 5m"
    fi

    local denied
    denied="$(echo "$log" | grep -ciE 'policy_denied|credential_endpoint_mismatch' || true)"
    if [ "${denied:-0}" -gt 0 ]; then
        warn "${denied} denied connection(s) — the pipeline tried to reach something"
        echo "$log" | grep -iE 'policy_denied|credential_endpoint_mismatch' \
            | tail -5 | sed 's/^/      /'
        info "if the destination is legitimate, add it to sandbox/policy.yaml"
        info "deliberately — do not work around it in code"
    else
        ok "no denied connections"
    fi
}

main() {
    printf '%s\n' "$BOLD"
    echo "  InkWell — pipeline in the sandbox"
    printf '%s%s\n\n' "$RESET" ""

    check_sandbox
    local name
    name="$(upload_document | tail -1)"

    if [ "$CHECK_ONLY" -eq 1 ]; then
        run_plumbing_check
        echo
        step "Done"
        info "the sandbox is reachable, the document uploaded, a real model"
        info "call succeeded, and /opt/data is writable."
        info "the pipeline itself has not run yet. Run it with:"
        info "  bash sandbox/run_pipeline.sh ${DOCUMENT}"
        return 0
    fi

    run_pipeline "$name"
    show_egress
    return 0
}

main "$@"
