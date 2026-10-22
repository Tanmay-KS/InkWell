# Ledger — architecture

Status: stub. This file becomes the source for the README's architecture
section and the video's diagram (Phase 4). The shape below is the one the
Phase 0 research settled on; it will be updated as phases land.

---

## The one-paragraph version

Ledger is a personal agent that reads your documents and tells you what is
about to expire, renew, or lapse — and drafts the action. It runs inside an
NVIDIA OpenShell sandbox with a declarative egress policy, and every request
that leaves the sandbox passes through a privacy router that strips personal
details, so the expensive models and third-party search engines never see
them. The agent itself is Hermes (Nous Research), which supplies persistent
household memory, a messaging gateway, and a skills system.

---

## Diagram

```
                    ┌──────────────────────────────────────────┐
                    │  User (Telegram / Slack / web view)      │
                    └───────────────────┬──────────────────────┘
                                        │
                    ┌───────────────────▼──────────────────────┐
                    │  OpenShell gateway                        │
                    │  · control plane, policy, credentials    │
                    └───────────────────┬──────────────────────┘
                                        │
              ┌─────────────────────────▼─────────────────────────┐
              │  Sandbox  (Docker, kernel-isolated)              │
              │                                                   │
              │   Hermes Agent  ─────────────────────────────┐   │
              │   · persistent memory (household picture)     │   │
              │   · skills, cron, messaging gateway          │   │
              │   · agent loop                               │   │
              │                                                │   │
              │   agent/                                       │   │
              │   · ingestion/  parse_documents.py   (OCR)    │   │
              │   · ingestion/  parse_email.py               │   │
              │   · memory/     hermes_memory.py             │   │
              │   · deadlines/  flag_deadlines.py            │   │
              │   · actions/    draft_actions.py             │   │
              └────────────────────────┬───────────────────────┘   │
                                       │                           │
       ┌───────────────────────────────▼──────────────────────┐    │
       │  Sandbox network proxy (egress enforcement)          │    │
       │  · network policy: which binary → which host         │    │
       │  · credential injection at the boundary              │    │
       │  · supervisor middleware: privacy router  ◄── Phase 2│    │
       └───────┬──────────────────────┬───────────────────────┘    │
               │                      │                            │
               │                      │                            │
   ┌───────────▼───────────┐  ┌───────▼────────────────────┐      │
   │  privacy_router/      │  │  search/tavily_client.py   │      │
   │  · redact.py          │  │  (Tavily, via the router)  │      │
   │  · injection_guard.py │  └────────────────────────────┘      │
   │  · audit_log.py       │                                       │
   └───────────┬───────────┘                                       │
               │                                                   │
               └──────────────────────┬────────────────────────────┘
                                      │
                    ┌─────────────────▼──────────────────┐
                    │  Nebius Token Factory               │
                    │  Nano (redaction) / Super / Ultra    │
                    │  Nano-Omni (OCR)                     │
                    └─────────────────────────────────────┘
```

---

## The data flow

1. A document (PDF, image, email) arrives — in the demo, from a file drop or
   a forwarded email.
2. `agent/ingestion/parse_documents.py` extracts text **locally** (tesseract
   for a scan, poppler for a PDF's text layer) and then redacts it
   **locally**. The document it hands on is already redacted, so anything
   that reasons about it — a model included — never sees a personal detail.
3. `agent/memory/hermes_memory.py` records what the household already knows
   and what has already been handled, and it is where the household's names
   come from — so the redactor is *told* the names rather than having to
   recognise them in prose.
4. `agent/deadlines/flag_deadlines.py` derives dated obligations, each one
   carrying the exact line of text it came from.
5. `agent/actions/draft_actions.py` drafts the response — a cancellation
   email, a refund request, a renewal reminder. The draft has the *real*
   values in it, because it is for the user and the user's channel is
   inside the boundary.
6. The result goes back to the user through Hermes's messaging gateway or the
   web view.

Every outbound request on this path crosses the OpenShell network proxy,
where the privacy router (Phase 2) intercepts it.

**The whole pipeline runs without a model call.** That is the design, not a
fallback: a renewal notice that says "your policy renews on 2027-03-07" does
not need a language model, and a pipeline that does not need one cannot leak
anything to one. The model is used where the text is prose rather than a
form, and even then it only ever sees redacted text.

The redaction map — the placeholder → real value table — is what lets step 5
write a draft with the real account number in it while the model that
helped never saw one. It never leaves the sandbox, and it is not part of
the document's serialisable form.

---

## The three proofs

The video needs three numbers. Each maps to a place in this tree.

| Proof | Lives in | What it shows |
|---|---|---|
| Leak rate | `eval/leak_test.py` | how often personal details appear in outbound payloads with the router ON vs OFF |
| Memory accuracy | `eval/memory_updates_eval.py` | accuracy on questions where the underlying fact changed, not just recall |
| Cost / latency per tier | `eval/cost_latency_bench.py` | the price of routing across Nano / Super / Ultra |

---

## Design decisions worth remembering

**The sandbox is the privacy boundary, and the router is the policy inside
it.** OpenShell decides *where* traffic may go; the router decides *what*
may be in it. A configuration discipline that says "remember not to put
account numbers in a search query" is not a privacy control. A runtime that
refuses the request is.

**Redaction is a middleware, not a library.** A library only sees the calls
we wrote. A supervisor middleware sees every request the sandbox is allowed
to make — including the ones Hermes makes on its own, and the ones a prompt
injection tries to make.

**The three model tiers are a cost decision, not a capability ladder.**
Nano handles redaction and classification. Super handles ordinary agent
reasoning. Ultra is reserved for the hardest work. The Phase 3 benchmark
turns that into a table.

**Personal details are not the same problem as secrets.** Hermes already
redacts credential-shaped strings from tool output before they reach the
model. The router handles personal data, which no existing layer touches.

---

## Ownership

| Area | Owner | Directory |
|---|---|---|
| Infra / privacy | Person A | `sandbox/`, `privacy_router/`, `jobs/`, `search/` |
| Agent / product | Person B | `agent/`, `interface/` |
| Joint | — | `eval/`, `docs/` |

---

## Open questions

- [ ] Does the redaction model call (Nano) count as "outbound PII" in the
      leak test, or is it a separate line item? The honest answer is a
      separate line item, with the claim scoped to "personal details never
      reach a Super/Ultra tier or a third-party service."
- [ ] Should the redaction run on the gateway host (middleware) or inside the
      sandbox? The middleware can only be reached by the supervisor, so
      running it on the host means PII transits host → Token Factory for the
      Nano call but never sandbox → Super/Ultra.
- [ ] Does Hermes's `auxiliary.vision` slot point at the OCR model, or does
      the ingestion pipeline call the model directly? The pipeline should
      call it directly — the auxiliary slot is for ad-hoc image analysis.
- [ ] Message-passing: is the Telegram gateway the primary interface, with
      the web view as the demo URL? The plan says both.
