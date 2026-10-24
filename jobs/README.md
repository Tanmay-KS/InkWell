# Deadline monitor job

Phase 3 — Person A.

`jobs/deadline_monitor.py` is the Nebius Serverless Jobs entrypoint for the
background check that tells the household what is coming due. It is a thin
shell over `HouseholdMemory`:

```text
HouseholdMemory.due_within(look_ahead_days)
        │
        ├── skip dismissed / handled items
        ├── skip items with a previous "notified" event
        ├── send a notice through the configured notifier
        ├── record the "notified" event on success
        └── write a small, non-personal job-state file
```

The SQLite handled-event table is the idempotency ledger. The job-state
JSON file is diagnostic metadata only; losing it must not cause a
notification to be repeated.

## Run locally

A dry run still reads the memory database and returns the due items, but it
does not send anything and does not mark anything as handled:

```bash
make deadline-monitor-dry-run
```

Or directly, with an isolated database and state file:

```bash
.venv/bin/python -m jobs.deadline_monitor \
  --dry-run \
  --today 2026-10-20 \
  --memory-db /tmp/ledger-memory.db \
  --state-path /tmp/ledger-job-state.json
```

A live local run uses the safe stdout notifier unless a command notifier is
configured:

```bash
.venv/bin/python -m jobs.deadline_monitor \
  --today 2026-10-20 \
  --memory-db /tmp/ledger-memory.db \
  --state-path /tmp/ledger-job-state.json
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEDGER_MEMORY_DB` | Hermes sidecar default | SQLite database containing household items |
| `LEDGER_STATE_PATH` | `/opt/data/state/job_state.json` | Non-personal job run metadata |
| `LEDGER_LOOK_AHEAD_DAYS` | `14` | How many days ahead count as due |
| `LEDGER_NOTIFY` | `true` | Send notifications; set `false` for a dry run |
| `LEDGER_HOUSEHOLD_ID` | `default` | Household identifier recorded in logs/state |
| `LEDGER_NOTIFY_COMMAND` | empty | Executable that receives the full notice as JSON on stdin |
| `LEDGER_NOTIFY_TIMEOUT_SECONDS` | `30` | Maximum time for one command-notifier attempt |

CLI flags override the environment for local runs:

```text
--look-ahead-days N
--today YYYY-MM-DD
--memory-db PATH
--state-path PATH
--notify-command COMMAND
--notify-timeout SECONDS
--notify / --no-notify
--dry-run
```

`--dry-run` always wins over `--notify`.

## Notification safety

There are two payloads:

* **Full notice** — includes `holder`, `reference`, `amount`, and the
  renewal date. It is passed to the notifier, because it is on its way to
  the user's own channel.
* **Safe summary** — includes only `item_id`, `kind`, `provider`,
  `renewal_date`, `days_until`, and `notified`. This is what `run_once()`
  returns, what the stdout notifier logs, and what the state file records.

The default stdout notifier is therefore safe for Serverless Job logs. A
command notifier is the integration seam for Telegram/Slack and receives
the full JSON notice on stdin.

A notification is recorded as handled only after the notifier returns
successfully. If the notifier fails, the item remains unhandled and the
next run retries it. A crash between a successful send and the event write
can produce one duplicate; at-least-once delivery is the deliberate
trade-off for not losing a deadline.

## Container image

Build from the repository root:

```bash
docker build -f jobs/Dockerfile -t ledger-deadline-monitor .
```

Run with a persistent volume mounted at `/data`:

```bash
docker run --rm \
  -v ledger-job-data:/data \
  -e LEDGER_LOOK_AHEAD_DAYS=14 \
  -e LEDGER_NOTIFY=false \
  ledger-deadline-monitor
```

The image runs as UID 10000. The mounted volume must be writable by that
UID.

## Nebius Serverless Jobs

The schedule lives on the Nebius side, not in this repository. A deployment
needs:

1. The job image pushed to a registry Nebius can pull from.
2. A persistent volume mounted at `/data`.
3. `LEDGER_MEMORY_DB=/data/ledger_memory.db`.
4. `LEDGER_STATE_PATH=/data/state/job_state.json`.
5. Either `LEDGER_NOTIFY_COMMAND` for delivery, or the default stdout
   notifier for a log-only proof.
6. A schedule or a manual trigger.

### Live proof checklist

This is the Phase 3 exit evidence. Do not mark it complete from the
schedule alone.

1. Seed one item due within the look-ahead window.
2. Trigger the job manually.
3. Record the job/run ID, status, and the log line containing one flagged
   item.
4. Trigger it a second time.
5. Record that the second run flags zero new items.
6. Put the redacted evidence in `docs/phase3-results.md`.

If Nebius access or credentials are unavailable, record the result as
**implemented and locally verified; live gate blocked**.

## Tests

```bash
.venv/bin/python -m pytest tests/test_deadline_monitor.py
```

The tests use temporary SQLite databases, fake notifiers, and no network.
They cover due-window filtering, dismissed and handled items, idempotency,
notifier failure, dry-run behavior, state-file safety, and both notifier
adapters.
