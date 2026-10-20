#!/usr/bin/env bash
# Ledger — OpenShell + Hermes + Token Factory setup.
#
# Idempotent: safe to re-run at any point. Each step checks whether it has
# already been done and skips it if so.
#
#   bash sandbox/setup.sh              # everything
#   bash sandbox/setup.sh --dry-run    # verify the setup without creating anything
#   bash sandbox/setup.sh --up         # create/refresh the sandbox only
#   bash sandbox/setup.sh --check      # run the routing verification only
#
# This script is the README's setup section, written as code so it cannot
# drift from reality. If you fix something by hand, fix it here too.
#
# Requires: bash, docker, curl, jq (optional), and a NEBIUS_API_KEY in the
# environment or in .env at the repo root.

set -euo pipefail

# --- paths ------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

GATEWAY_TOML="${SCRIPT_DIR}/gateway.toml"
POLICY_YAML="${SCRIPT_DIR}/policy.yaml"
PROVIDER_DIR="${SCRIPT_DIR}/providers"
DOCKERFILE="${SCRIPT_DIR}/Dockerfile.hermes"

IMAGE_NAME="${LEDGER_IMAGE_NAME:-ledger-hermes:local}"
VOLUME_NAME="${LEDGER_VOLUME_NAME:-ledger-hermes-data}"
SANDBOX_NAME="${LEDGER_SANDBOX_NAME:-ledger-hermes}"
GATEWAY_NAME="${LEDGER_GATEWAY_NAME:-local}"
GATEWAY_URL="${LEDGER_GATEWAY_URL:-https://127.0.0.1:17670}"
OPENSHELL_VERSION="${OPENSHELL_VERSION:-v0.1.4}"

# --- behaviour --------------------------------------------------------------

DRY_RUN=0
ONLY_UP=0
ONLY_CHECK=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --up)      ONLY_UP=1 ;;
        --check)   ONLY_CHECK=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

# --- output -----------------------------------------------------------------

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; BLUE=$'\033[36m'; RESET=$'\033[0m'

step()  { printf '%s==>%s %s%s%s\n' "$BLUE" "$RESET" "$BOLD" "$*" "$RESET"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail()  { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; }
info()  { printf '  %s%s%s\n' "$DIM" "$*" "$RESET"; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        info "would run: $*"
        return 0
    fi
    "$@"
}

die() { fail "$*"; exit 1; }

# --- environment ------------------------------------------------------------

# Load .env from the repo root if it exists, without clobbering variables
# that are already exported in the caller's environment.
if [ -f "${REPO_ROOT}/.env" ]; then
    info "loading ${REPO_ROOT}/.env"
    set -a
    # shellcheck disable=SC1091
    . "${REPO_ROOT}/.env"
    set +a
fi

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required but not on PATH"
}

# ============================================================================
# Step 1 — OpenShell
# ============================================================================

install_openshell() {
    step "OpenShell"

    if command -v openshell >/dev/null 2>&1; then
        ok "already installed: $(openshell --version 2>/dev/null || echo 'version unknown')"
        return
    fi

    # The upstream installer supports exactly three Linux paths: a Debian
    # package, an RPM package, or the snap. On a distribution with neither
    # dpkg nor rpm (Arch, CachyOS, Gentoo) the installer exits with
    # "Linux installs require either dpkg or rpm".
    #
    # The RPM route still works: the packages are plain archives, and
    # bsdtar (libarchive) reads RPMs. They contain the CLI, the gateway,
    # the prover, the systemd user unit and the config-migration helper —
    # everything except the container images, which the Docker driver pulls
    # from ghcr.io on first sandbox creation.
    if ! command -v dpkg >/dev/null 2>&1 && ! command -v rpm >/dev/null 2>&1; then
        install_openshell_from_rpm
        return
    fi

    info "installing from https://github.com/NVIDIA/OpenShell/releases (${OPENSHELL_VERSION})"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would run: curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh"
        return
    fi

    curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
    ok "installed"
}

install_openshell_from_rpm() {
    info "no dpkg and no rpm on this host — extracting the release RPMs instead"

    if ! command -v bsdtar >/dev/null 2>&1; then
        die "bsdtar is required to extract the RPMs on a non-deb/rpm host (pacman -S libarchive)"
    fi

    local arch pkg tmpdir file
    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64" ;;
        aarch64|arm64) arch="aarch64" ;;
        *) die "no OpenShell release is published for architecture: $(uname -m)" ;;
    esac

    tmpdir="$(mktemp -d)"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would download and extract the openshell, openshell-gateway and openshell-prover ${OPENSHELL_VERSION} rpms"
        info "would run: systemctl --user daemon-reload && systemctl --user enable --now openshell-gateway"
        rm -rf "$tmpdir"
        return
    fi

    for pkg in openshell openshell-gateway openshell-prover; do
        file="${pkg}-${OPENSHELL_VERSION#v}-1.fc44.${arch}.rpm"
        info "downloading ${file}"
        curl -LsSf --retry 3 -o "${tmpdir}/${file}" \
            "https://github.com/NVIDIA/OpenShell/releases/download/${OPENSHELL_VERSION}/${file}" \
            || { rm -rf "$tmpdir"; die "failed to download ${file}"; }
        bsdtar xf "${tmpdir}/${file}" -C / || { rm -rf "$tmpdir"; die "failed to extract ${file}"; }
    done
    rm -rf "$tmpdir"
    ok "OpenShell binaries installed"

    # The packaged unit generates the mTLS PKI and seeds gateway.toml on
    # first start. Where systemd --user is unavailable, the gateway has to
    # be run directly.
    if command -v systemctl >/dev/null 2>&1 \
       && systemctl --user is-system-running >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user enable --now openshell-gateway
        ok "gateway user service started"
    else
        warn "systemd --user is unavailable — start the gateway manually:"
        warn "  openshell-gateway generate-certs --output-dir ~/.local/state/openshell/tls --server-san host.openshell.internal"
        warn "  openshell-gateway --config ~/.config/openshell/gateway.toml"
    fi
}

# The OpenShell gateway creates its Docker client during startup, so a
# reachable Docker daemon is a prerequisite for the gateway itself — not
# just for creating sandboxes.
preflight_docker() {
    step "Docker"

    if ! command -v docker >/dev/null 2>&1; then
        die "docker is not on PATH — the OpenShell gateway will not start without it"
    fi

    if docker info >/dev/null 2>&1; then
        ok "daemon reachable (server $(docker version --format '{{.ServerVersion}}' 2>/dev/null))"
        return
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        info "docker daemon is not running — would report this as a blocker"
        return
    fi

    fail "the Docker daemon is not reachable"
    info "the OpenShell gateway creates its Docker client at startup and exits without it"
    info "fix it with:  sudo systemctl start docker"
    if ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        info "and:          sudo usermod -aG docker \$USER   (then log out and back in)"
    fi
    die "Docker is required before the rest of the setup can run"
}

register_gateway() {
    step "Gateway"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would copy ${GATEWAY_TOML} -> ~/.config/openshell/gateway.toml"
        info "would run: openshell gateway add ${GATEWAY_URL} --local --name ${GATEWAY_NAME}"
        info "would run: openshell gateway select ${GATEWAY_NAME}"
        return
    fi

    local gw_dir="${HOME}/.config/openshell"
    mkdir -p "$gw_dir"
    if [ -f "${gw_dir}/gateway.toml" ] && ! cmp -s "$GATEWAY_TOML" "${gw_dir}/gateway.toml"; then
        cp "${gw_dir}/gateway.toml" "${gw_dir}/gateway.toml.bak.$(date +%Y%m%d%H%M%S)"
        warn "existing gateway.toml differs — backed it up and overwriting"
    fi
    cp "$GATEWAY_TOML" "${gw_dir}/gateway.toml"

    if command -v openshell-gateway >/dev/null 2>&1; then
        openshell-gateway config preflight --path "${gw_dir}/gateway.toml" \
            || die "gateway.toml failed preflight — fix ${GATEWAY_TOML}"
        ok "gateway.toml passed preflight"
    else
        warn "openshell-gateway not found — skipping preflight"
    fi

    # Restart the user service if it is managed by systemd; otherwise the
    # gateway may already be running from the installer.
    if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active openshell-gateway >/dev/null 2>&1; then
        systemctl --user restart openshell-gateway
        ok "gateway service restarted"
    fi

    if ! openshell gateway list 2>/dev/null | grep -q "${GATEWAY_NAME}"; then
        openshell gateway add "${GATEWAY_URL}" --local --name "${GATEWAY_NAME}" || warn "gateway add failed"
    fi
    openshell gateway select "${GATEWAY_NAME}" || warn "gateway select failed"
    ok "gateway ${GATEWAY_NAME} registered"
}

# ============================================================================
# Step 2 — provider profiles
# ============================================================================

import_profiles() {
    step "Provider profiles"

    local profile
    for profile in "${PROVIDER_DIR}"/*.yaml; do
        [ -e "$profile" ] || continue
        local id
        id="$(grep -E '^id:' "$profile" | head -1 | awk '{print $2}')"

        if openshell profile list 2>/dev/null | grep -qE "(^|[[:space:]])${id}([[:space:]]|$)"; then
            ok "${id}: already imported"
        else
            if [ "$DRY_RUN" -eq 1 ]; then
                info "would run: openshell profile lint -f ${profile}"
                info "would run: openshell profile import -f ${profile}"
            else
                openshell profile lint -f "$profile" || die "profile lint failed for ${profile}"
                openshell profile import -f "$profile" || die "profile import failed for ${profile}"
                ok "${id}: imported"
            fi
        fi
    done
}

create_providers() {
    step "Provider instances"

    if [ -z "${NEBIUS_API_KEY:-}" ]; then
        warn "NEBIUS_API_KEY is not set — cannot create the Token Factory provider."
        warn "The sandbox will start, but every model call will fail with"
        warn "credential_endpoint_mismatch (no credential bound to the endpoint)."
        warn "Set it in .env and re-run this script."
        return
    fi

    if openshell provider list 2>/dev/null | grep -q 'nebius'; then
        ok "nebius: already created"
    else
        if [ "$DRY_RUN" -eq 1 ]; then
            info "would run: openshell provider create --name nebius --type nebius-token-factory --from-existing"
        else
            NEBIUS_API_KEY="$NEBIUS_API_KEY" openshell provider create \
                --name nebius --type nebius-token-factory --from-existing \
                || die "provider create failed"
            ok "nebius: created"
        fi
    fi

    if [ -n "${TAVILY_API_KEY:-}" ]; then
        if openshell provider list 2>/dev/null | grep -q 'tavily'; then
            ok "tavily: already created"
        else
            if [ "$DRY_RUN" -eq 1 ]; then
                info "would run: openshell provider create --name tavily --type tavily --from-existing"
            else
                TAVILY_API_KEY="$TAVILY_API_KEY" openshell provider create \
                    --name tavily --type tavily --from-existing \
                    || warn "tavily provider create failed"
                ok "tavily: created"
            fi
        fi
    else
        warn "TAVILY_API_KEY is not set — skipping the Tavily provider (Phase 2 will need it)"
    fi

    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
        if openshell provider list 2>/dev/null | grep -q 'telegram'; then
            ok "telegram: already created"
        else
            if [ "$DRY_RUN" -eq 1 ]; then
                info "would run: openshell provider create --name telegram --type telegram --from-existing"
            else
                TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" openshell provider create \
                    --name telegram --type telegram --from-existing \
                    || warn "telegram provider create failed"
                ok "telegram: created"
            fi
        fi
    else
        info "TELEGRAM_BOT_TOKEN is not set — skipping the Telegram provider (Phase 4 will need it)"
    fi
}

# ============================================================================
# Step 3 — image and volume
# ============================================================================

build_image() {
    step "Sandbox image"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would run: docker build -t ${IMAGE_NAME} -f ${DOCKERFILE} ${REPO_ROOT}"
        return
    fi

    docker build -t "$IMAGE_NAME" -f "$DOCKERFILE" "$REPO_ROOT" \
        || die "image build failed"
    ok "image ${IMAGE_NAME} built"
}

create_volume() {
    step "Data volume"

    # Hermes keeps sessions and memory in a WAL-mode SQLite database. Hermes's
    # own docs warn that a bind mount crossing a VM boundary (virtiofs on
    # macOS, 9p on Windows) can corrupt it silently — PRAGMA integrity_check
    # still passes while concurrent writers destroy the file. A named Docker
    # volume lives on the host's filesystem and does not have that problem.
    # Do NOT swap this for a bind mount.
    if [ "$DRY_RUN" -eq 1 ]; then
        info "would run: docker volume create --label openshell.ai/sandbox-attachable=true ..."
        info "would chown the volume to UID 10000"
        return
    fi

    docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1 \
        || docker volume create \
            --label openshell.ai/sandbox-attachable=true \
            --label openshell.ai/sandbox-attachable-workspace=default \
            "$VOLUME_NAME" >/dev/null
    ok "volume ${VOLUME_NAME} exists"

    # The container runs as UID 10000 (the image's `hermes` account). A fresh
    # volume is owned by root, so hand it over before the first boot.
    docker run --rm --user 0 \
        -v "${VOLUME_NAME}:/data" \
        --entrypoint sh \
        nousresearch/hermes-agent:latest \
        -c 'chown 10000:10000 /data' >/dev/null 2>&1 \
        || warn "could not chown the volume — the agent may fail to write state"
    ok "volume ownership set to 10000:10000"
}

# ============================================================================
# Step 4 — the sandbox
# ============================================================================

create_sandbox() {
    step "Sandbox"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would run: openshell sandbox create --name ${SANDBOX_NAME} --from ${IMAGE_NAME} ..."
        return
    fi

    if openshell sandbox list 2>/dev/null | grep -q "${SANDBOX_NAME}"; then
        ok "sandbox ${SANDBOX_NAME} already exists — recreating so it picks up policy changes"
        openshell sandbox delete "$SANDBOX_NAME" >/dev/null 2>&1 || true
    fi

    # The policy sets run_as_user 10000, so OpenShell will not accept the
    # image's root USER without it. The volume is attached via driver config,
    # which requires allow_driver_config and the attachable label.
    openshell sandbox create \
        --name "$SANDBOX_NAME" \
        --from "$IMAGE_NAME" \
        --policy "$POLICY_YAML" \
        --provider nebius \
        --driver-config-json "{\"docker\":{\"mounts\":[{\"type\":\"volume\",\"source\":\"${VOLUME_NAME}\",\"target\":\"/opt/data\",\"read_only\":false}]}}" \
        --restart-policy on-failure \
        --detach \
        -- chat \
        || die "sandbox creation failed"

    ok "sandbox ${SANDBOX_NAME} created"
}

attach_provider() {
    step "Provider attachment"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would run: openshell sandbox provider attach ${SANDBOX_NAME} nebius --wait"
        return
    fi

    openshell sandbox provider attach "$SANDBOX_NAME" nebius --wait \
        || warn "provider attach failed — check that the nebius provider exists"
    ok "nebius provider attached"
}

# ============================================================================
# Step 5 — verification
# ============================================================================

check_routing() {
    step "Routing verification"

    if [ "$DRY_RUN" -eq 1 ]; then
        info "would run: bash ${SCRIPT_DIR}/verify.sh"
        return 0
    fi

    # The checks live in verify.sh so there is exactly one implementation of
    # the Phase 0 exit condition — the same file a reviewer runs.
    bash "${SCRIPT_DIR}/verify.sh"
}

print_status() {
    step "Status"
    openshell sandbox list 2>/dev/null || true
    echo
    openshell policy get "$SANDBOX_NAME" --full 2>/dev/null | head -60 || true
}

# ============================================================================
# main
# ============================================================================

main() {
    printf '%s\n' "$BOLD"
    cat <<'BANNER'
  Ledger — OpenShell + Hermes + Nebius Token Factory
BANNER
    printf '%s%s\n' "$RESET" ""

    if [ "$ONLY_CHECK" -eq 1 ]; then
        check_routing
        return $?
    fi

    if [ "$ONLY_UP" -eq 0 ]; then
        preflight_docker
        install_openshell
        register_gateway
        import_profiles
        create_providers
        build_image
        create_volume
    fi

    create_sandbox
    attach_provider
    check_routing
    print_status

    step "Next"
    info "open a shell:   openshell sandbox exec -n ${SANDBOX_NAME} -- bash"
    info "watch traffic:  openshell term"
    info "tail logs:      openshell logs ${SANDBOX_NAME} --tail"
    info "the runbook is in docs/phase0-runbook.md"
}

main "$@"
