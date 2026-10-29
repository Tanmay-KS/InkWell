# Phase 3 — the three numbers

Phase 3 turns the three proofs into numbers that can go straight into the
video:

| Number | Produced by | Artifact |
|---|---|---|
| Leak rate, router OFF → ON | `make leak-test` | `eval/results/leak_test.json` |
| Memory-with-updates accuracy | `make memory-eval` | `eval/results/memory_eval.json` |
| Cost and p95 latency per tier | `make bench` | `eval/results/cost_latency.json` |
| Background job is really firing | `make job-status` | `$LEDGER_STATE_PATH` |

Every number is regenerable from the repository. Nothing in this document
is a hand-typed result.

---

## 1. Cost and latency per tier — `make bench`

### What it measures

Four request shapes the agent sends when it escalates to a model —
transcribe, extract a deadline, draft an action, record a memory update —
against each of the three tiers, five times each. A call records:

* **wall-clock latency**, per call, aggregated to a mean and a nearest-rank
  p95 (with 20 calls, the p95 is the 20th value — the tail is the point);
* **input and output tokens** from the response's `usage` block;
* **cost** from those token counts and the rate the live catalog publishes.

The benchmark sends placeholder text only. It is not a reason to put a
name or an account number into a prompt.

### Why the IDs cannot be hard-coded

`eval/cost_latency_bench.py` resolves each tier slot by role, not by ID:

1. the `LEDGER_MODEL_NANO/SUPER/ULTRA` pin in `.env`, if that exact ID is
   still in the live catalog;
2. otherwise the catalog's own matcher (`nano`, `super`, `ultra`),
   preferring a text-only model because the bench sends text.

A role with no match, or a match with no published price, is an error — not
a zero row. A cost table built on a guessed rate is a confident wrong
number, which is worse than no number.

### The current measurement

`make bench` on 2026-10-28, five repeats per prompt per tier, 60 calls,
30 models in the catalog, total measured spend **$0.0373**:

| Tier | Model | Calls | Errors | Out tok | Mean ms | p95 ms | Cost/call | $/1k calls |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 20 | 0 | 8,043 | 10,212 | 18,432 | $0.000103 | $0.10 |
| Super | `nvidia/Nemotron-3.5-Super-VL` | 20 | 0 | 14,939 | 3,544 | 6,945 | $0.000704 | $0.70 |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | 20 | 0 | 6,361 | 3,844 | 6,530 | $0.001059 | $1.06 |

The full record — every prompt, every per-prompt aggregate, the catalog
size, the pricing source, and the timestamp — is in
`eval/results/cost_latency.json`.

### What the table says

* **Cheapest is not fastest.** The Nano tier is the slowest of the three:
  ~10.2 s mean and ~18.4 s p95, against ~3.5 s and ~6.9 s for Super. The
  inversion held across two independent runs and every prompt, and it is
  not a token effect — Nano emitted *fewer* output tokens. Whatever the
  endpoint is doing with Nano, "use it because it is cheap" is not a
  latency argument.
* **Super costs ~6.9× Nano and ~0.7× Ultra per call**, because it emits a
  reasoning trace before it answers (14,939 output tokens for the same 20
  prompts). That is the honest cost of the tier the pipeline actually
  uses for reasoning.
* **A thousand calls are still cheap.** At $0.10–$1.06 per thousand, the
  tiering decision is about latency and quality, not about the bill.

### Reproducing

```bash
make bench-dry-run     # resolve the three tiers and prices; sends nothing
make bench             # 60 calls, writes eval/results/cost_latency.json
```

A transient TLS/5xx failure is retried once and counted in `retries`. A
4xx is not retried: it says something about the request, and retrying it
just doubles the wait. A tier whose every call fails is reported with its
errors rather than dropped from the table.

---

## 2. Background deadline monitoring — `make job`

### What the job does

`jobs/deadline_monitor.py` is the Nebius Serverless Jobs entrypoint. It is
a thin shell over the parts that already exist:

1. `HouseholdMemory.due_within(look_ahead_days)` — what renews inside the
   window, excluding anything the user dismissed and anything whose
   subscription status says cancelled/paused/expired;
2. a notifier (stdout by default; the messaging gateway passes its sender);
3. `record_event(item_id, "deadline_notified:<date>")` — the handled log,
   which is what makes a daily job idempotent;
4. a run record at `state_path` with `last_run`.

The date is part of the handled-log key on purpose. A provider that
re-issues a notice with a new renewal date is a new thing to say; a key
without the date would swallow it.

### What the notification does not contain

The record is `item_id`, `kind`, `provider`, `renewal_date`, `days_until`,
`amount`. The holder and the account reference are absent: this record is
written to a serverless job's stdout, which is a log. The gateway can look
the rest up from `item_id`.

### Running it

```bash
make job-dry-run       # report what is due; notify nothing, record nothing
make job              # one pass, as the scheduler does
make job-status       # is the schedule actually firing?
```

`--dry-run` deliberately does **not** write the run record or mark anything
handled. Otherwise a preview would silence the real notification, and
`job-status` would report a run that never notified anyone.

### Verifying it is live, not just scheduled

A schedule that exists but never fires looks identical to a working one in
the console. The run record is the difference:

```bash
# On the job host / data volume:
LEDGER_STATE_PATH=/opt/data/state/job_state.json \
  python -m jobs.deadline_monitor --status --max-age-hours 26
```

* exit **0** — `last_run` is inside the window; the schedule is firing;
* exit **1** — no record, an unreadable record, or a stale one. A daily job
  gets 36 hours by default so a late fire is not a false alarm.

The full check is: run it twice a day apart and confirm (a) `last_run`
advances, (b) the job log shows `flagged N item(s)`, and (c) the second run
does not re-notify the same renewal. The handled log is what makes (c)
true. A run that advances `last_run` but re-notifies is a failure of the
handled-log write, not of the scheduler.

### Status in this workspace

Implemented and tested (`tests/test_deadline_monitor.py`). The live
Serverless Job is **not** verified here: this host has no `nebius` CLI, no
service-account credentials in `.env`, and no Serverless Job spec in the
repository. Deploying one needs the project's Nebius account, a pushed
container image, and a subnet — none of which exist on this machine. The
Phase 3 checkbox stays unticked until `--status` returns 0 on a host where
the schedule runs.

---

## 3. Memory-with-updates accuracy — `make memory-eval`

### What it measures

Thirty scenarios, each a sequence of statements about a household fact
and a question whose answer depends on which statement is current. Every
scenario is isolated in its own SQLite store, so the statements are the
whole history the system has.

Three answer types, because they fail differently:

* **current** — the fact after a supersession ("is the plan active?");
* **superseded** — the fact *at a past date* ("what was it before the
  cancellation?");
* **never stated** — abstention on a subject the household never
  mentioned.

The `never_stated` query is not short-circuited. The evaluation asks the
store for the subject and scores the answer it gets; a subject that was
never recorded returns nothing, and a subject that *was* recorded would
fail. A short-circuit that returns "" without asking would score 100%
without testing anything, which is exactly the kind of number this eval
exists to avoid.

### The current measurement

`make memory-eval` on 2026-10-28, 30 scenarios across 8 categories:

| Question type | Ledger | Update-blind baseline |
|---|---:|---:|
| current fact after a supersession | **100.0%** | 15.8% |
| the superseded fact, at a point in time | **100.0%** | 83.3% |
| abstention on a never-stated subject | **100.0%** | 100.0% |
| **overall** | **100.0%** | **43.3%** |

| Category | Scenarios | Accuracy |
|---|---:|---:|
| status | 8 | 100% |
| amount | 5 | 100% |
| renewal_date | 5 | 100% |
| holder | 3 | 100% |
| contact | 3 | 100% |
| preference | 3 | 100% |
| confidence | 2 | 100% |
| restatement | 1 | 100% |

The baseline is what most memory demos ship: keep the first value you were
told and never revise it. It scores 15.8% on the current-fact questions —
it remembers *a* value, just not the right one — and 83.3% on the
superseded questions, because the first value happens to be the historical
one there. Overall 43.3% against 100%.

The full per-scenario record, including both baselines, is in
`eval/results/memory_eval.json`.

### What the score is not

It is **deterministic SQLite fact supersession**, not model paraphrase
quality. The number says the memory layer supersedes, preserves history,
refuses to overturn a high-confidence fact with a low-confidence note, and
does not invent a fact that was never stated. It does not say a language
model reading those facts would answer equally well.

It is also not a claim that every update shape is covered. Same-day
updates collapse to the day's final value by design (the append-only event
log keeps the individual actions), and a statement dated before the
current fact is refused rather than reordered. Neither is in this scenario
set; both are documented limits of the tracker, not silent passes.

### Adding a scenario

Append to `eval/scenarios/memory_updates.json`:

```json
{
  "scenario_id": "unique-id",
  "statements": [
    {"subject": "gym", "kind": "subscription_status", "value": "active", "valid_from": "2026-01-01"},
    {"subject": "gym", "kind": "subscription_status", "value": "cancelled", "valid_from": "2026-03-14"}
  ],
  "question": "What is the gym status now?",
  "expected": "cancelled",
  "answer_type": "current",
  "metadata": {"subject": "gym", "kind": "subscription_status", "as_of": "2026-10-20", "category": "status"}
}
```

`valid_from` is when the *fact* became true, not the date the fact
describes. Values are compared as strings, so keep the format (amounts as
`"162.40"`, dates as ISO) in both `value` and `expected`. Duplicate
scenario ids are an error. `tests/test_memory_updates_eval.py` runs the
canonical set, so a tracker regression fails the suite rather than quietly
lowering the published number.

---

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| `make bench` says `cannot resolve tiers` | a pinned model left the catalog and no fallback matches | `make model-catalog`, then update the pin |
| `make bench` says `no price` | the catalog record has no `pricing` | pick a tier with a published rate; do not guess |
| every bench call fails with 401 | wrong/missing `NEBIUS_API_KEY` | fix `.env`; `make bench-dry-run` needs it too, because it reads the catalog |
| `job-status` exits 1 with "never run" | wrong `LEDGER_STATE_PATH`, or the job has not fired | check the volume mount and the schedule |
| `job-status` exits 1 with "not firing" | the scheduler stopped, or the job crashes before it writes | read the job log; the record is written last, so a crash shows as staleness |
| the same renewal is notified twice | the handled-log write failed | check the memory DB is writable and not a bind mount (F-008) |
