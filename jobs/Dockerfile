# Ledger — deadline monitor job image.
#
# Phase 3 — Person A.
#
# Build from the repository root:
#   docker build -f jobs/Dockerfile -t ledger-deadline-monitor .
#
# The image contains only the modules the monitor imports. It deliberately
# does not inherit the Hermes sandbox image: this job reads the SQLite sidecar
# and emits a notification, so it has no OCR/vision/browser dependencies and
# does not need the privacy router.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN groupadd --gid 10000 ledger && \
    useradd --uid 10000 --gid 10000 --create-home ledger && \
    mkdir -p /data/state && \
    chown -R 10000:10000 /data

# Keep the COPY surface explicit. The monitor only imports the memory store
# and the notifier; everything else in agent/ is for the sandbox pipeline.
COPY agent/__init__.py /app/agent/
COPY agent/memory/__init__.py agent/memory/hermes_memory.py /app/agent/memory/
COPY jobs/__init__.py jobs/deadline_monitor.py jobs/notifier.py jobs/seed_due_item.py /app/jobs/

ENV LEDGER_MEMORY_DB=/data/ledger_memory.db \
    LEDGER_STATE_PATH=/data/state/job_state.json

USER 10000:10000

ENTRYPOINT ["python", "-m", "jobs.deadline_monitor"]
