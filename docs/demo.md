# The five-minute demo

Ledger's interface is a local web view over a fictional household. It
starts in one command and binds to loopback only.

```bash
make demo-web
# Ledger local interface: http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765> for the Ledger overview, then choose **Open Ledger** to enter the dashboard at `/app`. The dashboard header says **Demo · fictional data**.
The six seeded items are invented; the demo uses a temporary database
that is deleted when the process exits, so it never reads or writes the
real household sidecar at `~/.hermes/ledger_memory.db`.

## The tour

1. **Proactive findings.** The page opens on the Watcher's finding, not on
   a typed prompt: *2 deadline(s) this week* and *1 likely duplicate
   charge* (two Northstar Auto rows, same amount, two days apart). The
   Verifier computed both; no model was involved.
2. **Coming up.** Four items renew inside 30 days. *RiverFlix* is in the
   household list but not in *Coming up*, because it is cancelled. That
   distinction is the memory feature: a cancellation is a dated fact, not
   a deleted row.
3. **Ask Ledger.** Click *What's coming up?* or type
   `When does Orchard Fitness renew?`. The answer comes from the local
   database, not from a model call.
4. **Change a fact.** Type `Cancel Orchard Fitness`. The reply names the
   effective date, the chip changes to `cancelled`, and the item leaves
   *Coming up*. Type `Reactivate Orchard Fitness` to put it back. The old
   fact stays in the history with a closed validity window.
5. **The glass-box trace.** The trace panel shows every step of the
   seeded run: Redactor → Extractor → Verifier → Researcher → Drafter →
   **Auditor rejects a wrong amount** → revision → Auditor approves →
   human approval. Each row carries a tier badge, a latency, an egress
   badge and a status. The seeded run plants the wrong amount on purpose
   so the rejection is visible; it is a real run over fictional data, not
   a hand-written animation.
6. **Add a notice.** Upload a text, PDF, PNG or JPEG notice. The page
   extracts it locally, redacts it locally, runs the Verifier and the
   Auditor, tracks the item, and shows the full draft for you to review.
   The returned trace replaces the seeded one. Ledger does not send it.
7. **Look at the draft.** The draft is the only place personal details are
   shown, and it is shown only in this loopback view. The trace summaries
   and the chat preview are redacted.

## What this demo is, and is not

**It is** the real `agent/` pipeline, the real fact table, the real chat
handler, the real LangGraph role graph (Verifier/Auditor path) and the real
draft generator, driven by a deterministic fictional fixture
(`_seed_demo` in `interface/web/server.py`). The seeded trace is a real
`run_text_graph` call whose Drafter deliberately plants a wrong amount.

**It is not** the loopback server. The server refuses any `Host` header that
is not `127.0.0.1`, `::1` or `localhost`, and any cross-origin request. The
household database is never exposed.

There is a separate **public demo** deployment on Vercel. It runs the same
handler and routes through `interface/web/wsgi.py`, but its state is a
throwaway `/tmp` database seeded with the same fictional fixture. It never
reads the household sidecar, model drafting stays off, and everything
resets when the instance recycles. Only that entrypoint sets
`public_demo`, which is the sole reason the host check is bypassed.


**It is not** model-backed by default. The template Drafter is
deterministic and makes no model call, which is why the demo trace shows
`tier: template` and zero model calls. Setting
`LEDGER_USE_MODEL_DRAFTER=1` switches the upload path to Nemotron Super
through Token Factory; the deterministic Auditor then reviews the model's
draft. `make langgraph-gate` exercises that path once and records the
result in `eval/results/langgraph_gate.json`.

The sandbox-side proofs are separate and do not need the web view:
`make leak-test`, `make injection-demo`, `make results`.

## Resetting

The demo database is temporary. Stop the server and start it again with
`make demo-web` for a clean fixture.

To run against a real household database instead:

```bash
make web
# reads $LEDGER_MEMORY_DB, default ~/.hermes/ledger_memory.db
```

Both targets are loopback-only. There is no flag that changes that.
