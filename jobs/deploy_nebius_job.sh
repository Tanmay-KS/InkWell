#!/usr/bin/env bash
# Deploy or trigger the Ledger Deadline Monitor on Nebius Serverless Jobs.
#
# Usage:
#   bash jobs/deploy_nebius_job.sh [options]
#
# Options:
#   --manifest PATH     Path to job YAML manifest (default: jobs/nebius-job.yaml)
#   --image IMAGE       Container image URI (default: ghcr.io/tanmay-ks/ledger-deadline-monitor:latest)
#   --dry-run           Print generated command/manifest without executing
#   --trigger           Trigger an immediate manual run of an existing job
#   --help              Show this message

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MANIFEST_PATH="${SCRIPT_DIR}/nebius-job.yaml"
JOB_NAME="ledger-deadline-monitor"
IMAGE_URI="${LEDGER_JOB_IMAGE:-ghcr.io/tanmay-ks/ledger-deadline-monitor:latest}"
STORAGE_CLAIM="${LEDGER_STORAGE_CLAIM:-ledger-shared-storage}"
DRY_RUN=0
TRIGGER_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)
      MANIFEST_PATH="$2"
      shift 2
      ;;
    --image)
      IMAGE_URI="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --trigger)
      TRIGGER_ONLY=1
      shift
      ;;
    --help|-h)
      head -n 14 "$0" | tail -n +2
      exit 0
      ;;
    *)
      echo "Error: Unknown argument '$1'" >&2
      exit 1
      ;;
  esac
done

if [[ "$TRIGGER_ONLY" -eq 1 ]]; then
  echo "==> Triggering immediate run for job '${JOB_NAME}'..."
  CMD="nebius ai job run create --job-name ${JOB_NAME}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[DRY-RUN] Would run: $CMD"
    exit 0
  fi
  if ! command -v nebius >/dev/null 2>&1; then
    echo "Error: 'nebius' CLI is not installed or not in PATH." >&2
    echo "Hint: Install nebius CLI or run with --dry-run." >&2
    exit 1
  fi
  eval "$CMD"
  exit $?
fi

echo "==> Deploying Nebius Serverless Job '${JOB_NAME}'"
echo "    Image:    ${IMAGE_URI}"
echo "    Manifest: ${MANIFEST_PATH}"

if [[ ! -f "$MANIFEST_PATH" ]]; then
  echo "Error: Manifest file not found at ${MANIFEST_PATH}" >&2
  exit 1
fi

CMD=(
  nebius ai job create
  --name "$JOB_NAME"
  --image "$IMAGE_URI"
  --cpu "1"
  --memory "1Gi"
  --mount-volume "claim=${STORAGE_CLAIM},path=/data"
  --env "LEDGER_LOOK_AHEAD_DAYS=14"
  --env "LEDGER_MEMORY_DB=/data/ledger_memory.db"
  --env "LEDGER_STATE_PATH=/data/state/job_state.json"
  --env "LEDGER_NOTIFY=1"
)

if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  CMD+=(
    --env "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
    --env "TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}"
  )
fi

if [[ -n "${LEDGER_WEBHOOK_URL:-}" ]]; then
  CMD+=(--env "LEDGER_WEBHOOK_URL=${LEDGER_WEBHOOK_URL}")
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DRY-RUN] Manifest verified: ${MANIFEST_PATH}"
  echo "[DRY-RUN] Equivalent Nebius CLI command:"
  echo "  ${CMD[*]}"
  exit 0
fi

if ! command -v nebius >/dev/null 2>&1; then
  echo "Notice: 'nebius' CLI not found on this machine."
  echo "To apply declarative manifest when using Nebius Kubernetes/Batch API:"
  echo "  kubectl apply -f ${MANIFEST_PATH}"
  echo ""
  echo "Or using Nebius CLI on your gateway/control host:"
  echo "  ${CMD[*]}"
  exit 0
fi

"${CMD[@]}"
