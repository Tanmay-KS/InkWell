# Ledger — deadline monitor job image.
#
# Phase 3 — Person A.
#
# Build from the repository root:
#   docker build -f jobs/Dockerfile -t ledger-deadline-monitor .
#
# The image contains only the code the monitor needs. It deliberately does
# not inherit the Hermes sandbox image: this job reads the SQLite sidecar
# and emits a notification, so it has no OCR/vision/browser dependencies.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN useradd --uid 10000 --create-home ledger && \
    mkdir -p /data/state && \
    chown -R 10000:10000 /data

COPY agent /app/agent
COPY privacy_router /app/privacy_router
COPY jobs /app/jobs

ENV LEDGER_MEMORY_DB=/data/ledger_memory.db \
    LEDGER_STATE_PATH=/data/state/job_state.json

USER 10000

ENTRYPOINT ["python", "-m", "jobs.deadline_monitor"]
