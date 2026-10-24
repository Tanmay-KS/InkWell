# Phase 3 results — Person A

This file records the Person A half of Phase 3: the background deadline
monitor and the Nemotron tier cost/latency table. It is deliberately
separate from the Phase 0/2 evidence files because these are Phase 3
results and should be replaceable without rewriting the architecture notes.

## 1. Background deadline monitor

**Status:** implemented and locally verified. The live Nebius Serverless
Jobs gate is **not** claimed here.

### What was built

`jobs/deadline_monitor.py` is a one-pass Serverless Job entrypoint:

1. read the household sidecar, asking for items due within the look-ahead
   window (`HouseholdMemory.due_within`);
2. skip dismissed items, already-handled items, and items with a previous
   `notified` event in the handled log;
3. send each remaining item through an injected notifier;
4. record the `notified` event only after the notifier succeeds;
5. write a small, non-personal job-state file atomically.

The handled-event table is the idempotency source of truth. The JSON state
file is diagnostic only; a failure to write it does not lose a
notification or cause a retry.

Two notifiers ship:

* `stdout_notifier` writes `item_id`, provider, kind, renewal date, and
  days remaining to the job log. It never writes the holder or reference.
* `command_notifier` writes the full notice as JSON to an external command
  on stdin. It is the Telegram/Slack seam and never uses a shell.

### Local evidence

The focused suite covers due-window filtering (including the boundary),
dismissed/handled items, second-run idempotency, notifier failure and
retry, dry-run behavior, state-file safety, both notifier adapters, command
timeouts, and the CLI entrypoint:

```text
tests/test_deadline_monitor.py — 24 passed
full suite — 317 passed
ruff — clean
```

A local CLI run with an isolated SQLite database produced:

```text
{"days_until": 3, "event": "deadline_notification", "household_id": "default",
 "item_id": "item-2ddd23d7689df759", "kind": "subscription",
 "provider": "Smoke Provider", "renewal_date": "2026-10-23"}
ledger: flagged 1 item(s) for default
ledger: flagged 0 item(s) for default
```

The second line is the important one: the handled-event table made the
second run quiet. The holder and reference used in the smoke fixture do
not appear in the notification output or the state file.

### Live gate

The live Serverless Jobs proof requires a Nebius project, a job image, and
a persistent volume mounted at `/data`; none of those were available in
this environment. The container image was authored and the Dockerfile is
in `jobs/Dockerfile`, but the local Docker daemon was not running, so the
image was not built here.

The live checklist remains:

1. seed one item due inside the window;
2. trigger the job manually;
3. record job/run ID, status, and one flagged-item log line;
4. trigger it a second time;
5. record zero new items;
6. put the redacted evidence here.

**Do not mark the IMPLEMENTATION.md Serverless item complete until those
steps have run against Nebius.**

## 2. Cost and latency benchmark

`eval/cost_latency_bench.py` runs the same four representative prompts
(redaction, deadline extraction, action draft, memory update) against the
three pinned Nemotron tiers:

| Tier | Model |
|---|---|
| Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |
| Super | `nvidia/Nemotron-3.5-Super-VL` |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` |

Tiers are interleaved inside each prompt/repetition so a network change or
a warm cache cannot systematically favour whichever tier ran first. Cost
uses the token usage returned by the API and the rates in
`eval/pricing.json`; latency is wall-clock around one completion and p95
is reported across repetitions. Errors are counted without recording
their messages.

The offline test suite uses an injected transport and covers scheduling,
token accounting, error accounting, pricing validation, p95 calculation,
Markdown/JSON output, and the missing-key path:

```text
tests/test_cost_latency_bench.py — 27 passed
```

The live table is filled in below only when a live run actually completes.

### Live run

**Run:** 2026-10-24 20:31 UTC · 4 prompts × 3 repeats × 3 tiers ·
**36/36 calls succeeded, 0 errors** · `max_tokens=4096` ·
`temperature=0.0` · default Token Factory endpoint.

| Tier | Model | Calls | Errors | Mean | p50 | p95 | Tokens in | Tokens out | Cost (12 calls) | $/call |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 12 | 0 | 7.837s | 2.079s | 23.475s | 1,107 | 13,951 | $0.003415 | $0.000285 |
| Super | `nvidia/Nemotron-3.5-Super-VL` | 12 | 0 | 3.728s | 2.095s | 8.517s | 1,107 | 18,363 | $0.016859 | $0.001405 |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | 12 | 0 | 2.072s | 1.568s | 4.708s | 1,107 | 7,895 | $0.024792 | $0.002066 |

The identical input-token count confirms every tier received the same
prompts. The interesting result is that the cheapest tier was not the
fastest: Nano cost 1/5 of Super per call, but its mean and p95 latency were
higher because it produced the second-largest completion volume and had a
long tail. Ultra was the most expensive per call and the fastest in this
run, with the fewest output tokens.

That does not make Ultra the default. It makes the tiering decision a
three-way trade-off: Nano for short classification/redaction work where
the latency tail is acceptable, Super for the ordinary reasoning path, and
Ultra only where the task needs it. The benchmark measures cost and
latency, not answer quality; the quality decision stays with the task
evals.

The generated JSON and Markdown are in `eval/results/` locally and are
ignored by git. This table is the committed evidence. Refresh
`eval/pricing.json` from the published catalog before treating the dollar
figures as current.
