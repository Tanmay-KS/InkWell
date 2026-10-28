# The five-minute demo

Ledger's interface is a local web view over a fictional household. It
starts in one command and binds to loopback only.

```bash
make demo-web
# Ledger local interface: http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765>. The header says **Demo · fictional data**.
The four seeded items are invented; the demo uses a temporary database
that is deleted when the process exits, so it never reads or writes the
real household sidecar at `~/.hermes/ledger_memory.db`.

## The tour

1. **Coming up.** Three items renew inside 30 days. *RiverFlix* is in the
   household list but not in *Coming up*, because it is cancelled. That
   distinction is the memory feature: a cancellation is a dated fact, not
   a deleted row.
2. **Ask Ledger.** Click *What's coming up?* or type
   `When does Orchard Fitness renew?`. The answer comes from the local
   database, not from a model call.
3. **Change a fact.** Type `Cancel Orchard Fitness`. The reply names the
   effective date, the chip changes to `cancelled`, and the item leaves
   *Coming up*. Type `Reactivate Orchard Fitness` to put it back. The old
   fact stays in the history with a closed validity window.
4. **Add a notice.** Upload a text, PDF, PNG or JPEG notice. The page
   extracts it locally, redacts it locally, tracks the item, and shows the
   full draft for you to review. Ledger does not send it.
5. **Look at the draft.** The draft is the only place personal details are
   shown, and it is shown only in this loopback view. The chat preview is
   redacted.

## What this demo is, and is not

**It is** the real `agent/` pipeline, the real fact table, the real chat
handler and the real draft generator, driven by a deterministic fictional
fixture (`_seed_demo` in `interface/web/server.py`).

**It is not** a hosted service. The server refuses any `Host` header that
is not `127.0.0.1`, `::1` or `localhost`, and any cross-origin request. We
did not publish a public URL because doing so would put the household
database behind no authentication at all. The honest demo is the one you
run locally.

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
