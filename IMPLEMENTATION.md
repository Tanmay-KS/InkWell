# Ledger — Implementation Plan (Person A / Person B)

**Project:** Ledger — privacy-routed personal agent for deadlines, renewals, and entitlements
**Track:** Personal AI
**Deadline:** Oct 30, 2026, 10:00 AM PDT (target submit: Oct 29)
**Roles:** Person A = Infra/Privacy lead · Person B = Agent/Product lead

---

## Phase 0 — Infra Verification (Oct 19–20)

**Goal:** De-risk the stack before building anything user-facing.

### Person A
- [ ] Deploy/run Hermes Agent inside an NVIDIA OpenShell sandbox
- [ ] Confirm model calls from inside the sandbox actually route to Nebius Token Factory (not a local or default endpoint)
- [ ] Document exact auth/config steps needed (API keys, sandbox policy file, network rules) — this becomes the setup section of the README later
- [ ] Flag any blocking issue immediately to Person B

### Person B
- [ ] Identify available OCR/vision models on Token Factory (or confirm fallback, e.g. an open OCR library) for document ingestion
- [ ] Set up repo skeleton: folder structure, `.env.example`, basic CI/lint if time allows
- [ ] Draft ~200 synthetic documents containing fake personal details (names, account numbers, addresses, renewal dates) — needed later for the Phase 2 leak test, start early since it's tedious
- [ ] Start a shared friction log doc (both people append to it all week)

**Exit condition (joint):** Hermes+OpenShell+Token Factory confirmed working by EOD Oct 20. If not, jointly decide to pivot to the simpler/fallback stack before Phase 1 starts.

---

## Phase 1 — Core Pipeline (Oct 21–23)

**Goal:** Working end-to-end agent loop: document in → deadline flagged → draft action out.

### Person B (lead)
- [ ] Build document/email ingestion (parsing, chunking, extraction using the OCR/vision model from Phase 0)
- [ ] Build persistent memory layer in Hermes: household picture, what's already been handled
- [ ] Build deadline-flagging logic from extracted document data
- [ ] Build first draft-action generation (e.g. "draft cancellation email," "draft refund request")

### Person A
- [ ] Wire Hermes/OpenShell so B's pipeline runs on the real sandbox from day one, not bolted on later
- [ ] Start scaffolding egress policy rules in OpenShell (what's allowed to leave the sandbox, to where) — not enforcing yet, just structure
- [ ] Review B's pipeline for anywhere sensitive data might leak outside the sandbox unguarded

**Exit condition:** Feed a document in, get a flagged deadline + a draft action out the other side, running inside the sandbox.

---

## Phase 2 — Privacy Router (Oct 24–25)

**Goal:** Build the project's actual technical differentiator.

### Person A (lead)
- [ ] Implement Nemotron Nano redaction layer — strips personal details before data reaches Super/Ultra models or Tavily
- [ ] Implement OpenShell egress policy enforcement (block/allow rules on outbound traffic)
- [ ] Build the live prompt-injection-block demo scenario: untrusted message tries to exfiltrate data, sandbox stops it on screen, produces an audit log entry

### Person B
- [ ] Build the leak-test harness: run the ~200 synthetic docs (from Phase 0) through the pipeline with the router ON and OFF
- [ ] Measure and log how often sensitive details appear in outbound queries in each condition
- [ ] Package results into a clear before/after comparison (table or chart) for the video

**Exit condition:** Real leak-rate number (with/without router), injection block reproducible on screen with audit log.

---

## Phase 3 — Evaluation (Oct 26–27)

**Goal:** Turn the three proofs into reportable numbers for the video.

### Person B
- [ ] Build memory-with-updates eval: test questions where underlying facts changed (e.g. "I cancelled that plan in March")
- [ ] Score accuracy, since most demos only test simple recall — this is meant to stand out

### Person A
- [ ] Verify Nebius Serverless Jobs background deadline monitoring is actually running live (not just scheduled)
- [ ] Benchmark cost and latency per Nemotron tier (Nano / Super / Ultra) across a representative set of calls

### Joint
- [ ] Compile the three results (leak rate, memory accuracy, cost/latency table) into video-ready visuals

**Exit condition:** 3 numbers/charts ready to drop into the video.

---

## Phase 4 — Packaging (Oct 28)

**Goal:** Make the project submittable by a stranger.

### Person B
- [ ] Polish the chat interface (Telegram/Slack) and simple web view
- [ ] Test demo URL fully logged-out, fix any auth-wall issues

### Person A
- [ ] Write README: highlight Nemotron + Token Factory usage, setup steps, architecture overview
- [ ] Write up the friction log into the feedback section required by the rules
- [ ] Add open-source license to repo

### Joint
- [ ] Cross-review each other's output (A reviews B's UI/demo, B reviews A's README/docs) before EOD

**Exit condition:** A stranger could open the repo and demo URL and understand it in 5 minutes.

---

## Phase 5 — Record & Submit (Oct 29)

**Goal:** Ship with margin before the Oct 30, 10:00 AM PDT deadline.

### Joint
- [ ] Script the 3-minute video together:
  - Person A narrates: privacy router, leak test, injection block, cost/latency
  - Person B narrates: product walkthrough, memory-with-updates demo, UI
- [ ] Record with a backup take
- [ ] One person owns final submission checklist: demo URL, video, public repo (license included), README, written feedback
- [ ] Submit by Oct 29 (buffer day before the Oct 30 deadline)

---

## Repo File Structure

```
ledger/
├── README.md                      # Setup, Nemotron/Token Factory usage, friction log (Person A)
├── LICENSE
├── .env.example
│
├── sandbox/                       # OpenShell sandbox config + policy (Person A)
│   ├── openshell.config.yaml
│   ├── egress_policy.yaml         # Phase 2: outbound allow/block rules
│   └── setup.sh                   # Hermes + Token Factory routing setup (Phase 0)
│
├── privacy_router/                # Phase 2 core differentiator (Person A)
│   ├── redact.py                  # Nemotron Nano redaction layer
│   ├── injection_guard.py         # Live prompt-injection block logic
│   └── audit_log.py               # Audit trail for blocked/redacted events
│
├── agent/                         # Core pipeline (Person B)
│   ├── ingestion/
│   │   ├── parse_documents.py     # OCR/vision extraction (Phase 1)
│   │   └── parse_email.py
│   ├── memory/
│   │   ├── hermes_memory.py       # Persistent household memory
│   │   └── update_tracker.py      # Phase 3: memory-with-updates logic
│   ├── deadlines/
│   │   └── flag_deadlines.py
│   └── actions/
│       └── draft_actions.py       # Draft-action generation
│
├── jobs/                          # Background monitoring (Person A)
│   └── deadline_monitor.py        # Nebius Serverless Jobs entrypoint
│
├── search/                        # Tavily integration (Person A, routed through privacy_router)
│   └── tavily_client.py
│
├── interface/                     # Chat + web view (Person B)
│   ├── bot/                       # Telegram/Slack bot
│   │   └── handlers.py
│   └── web/                       # Simple web view
│       ├── index.html
│       └── app.js
│
├── eval/                          # Phase 2–3 proofs (Joint)
│   ├── synthetic_docs/            # ~200 synthetic documents (Person B, Phase 0)
│   ├── leak_test.py               # Router on/off comparison (Person B)
│   ├── memory_updates_eval.py     # Memory-with-updates accuracy (Person B)
│   └── cost_latency_bench.py      # Per-tier benchmarking (Person A)
│
├── docs/
│   ├── friction_log.md            # Ongoing, both append (Phase 0 start)
│   └── architecture.md            # Diagram + explanation for README/video
│
└── video/
    └── script.md                  # Phase 5 narration script (Joint)
```

**Ownership at a glance:** `sandbox/`, `privacy_router/`, `jobs/`, `search/` → Person A. `agent/`, `interface/` → Person B. `eval/` and `docs/` are split per-file as noted above but live together so both people can see proof status at a glance.

---

## Key dependency note

Phase 1 → Phase 2 is the one hard dependency: the router needs a working pipeline to route. The Phase 0 joint scouting (OCR models + synthetic docs) is designed to absorb that risk early so Phase 2 doesn't start from zero.
