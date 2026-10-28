# Ledger — Implementation Plan (Person A / Person B)

**Project:** Ledger — privacy-routed personal agent for deadlines, renewals, and entitlements
**Track:** Personal AI
**Deadline:** Oct 30, 2026, 10:00 AM PDT (target submit: Oct 29)
**Roles:** Person A = Infra/Privacy lead · Person B = Agent/Product lead

> **Upgrade Phase (Oct 29):** the additive glass-box multi-agent plan is at
> the end of this file. Its checked boxes are an evidence-based gap analysis
> of what already exists — the upgrade itself is not implemented.

---

## Phase 0 — Infra Verification (Oct 19–20)

**Goal:** De-risk the stack before building anything user-facing.

### Person A
- [x] Deploy/run Hermes Agent inside an NVIDIA OpenShell sandbox
- [x] Confirm model calls from inside the sandbox actually route to Nebius Token Factory (not a local or default endpoint)
- [x] Document exact auth/config steps needed (API keys, sandbox policy file, network rules) — this becomes the setup section of the README later
- [x] Flag any blocking issue immediately to Person B

### Person B
- [x] Identify available OCR/vision models on Token Factory (or confirm fallback, e.g. an open OCR library) for document ingestion
- [x] Set up repo skeleton: folder structure, `.env.example`, basic CI/lint if time allows
- [x] Draft ~200 synthetic documents containing fake personal details (names, account numbers, addresses, renewal dates) — needed later for the Phase 2 leak test, start early since it's tedious
- [x] Start a shared friction log doc (both people append to it all week)

**Exit condition (joint):** Hermes+OpenShell+Token Factory confirmed working by EOD Oct 20. If not, jointly decide to pivot to the simpler/fallback stack before Phase 1 starts.

**Status (closed 2026-10-21, one day past the EOD Oct 20 target).** The exit
condition is met: Hermes v0.22.2 ran inside the `ledger-hermes` sandbox, a
real inference call on `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` returned
`LEDGER-OK`, the effective policy carries the provider rule naming the
actual Python interpreter, and an outbound request to `https://example.com/`
was refused. Evidence and the per-item breakdown are in
`docs/phase0-runbook.md`; the full record of what broke is in
`docs/friction_log.md`.

**Carried into Phase 1 — not blocking, but open:**

- `sandbox/verify.sh` is the automated form of the exit condition and must
  be run **on the gateway host**, where the CLI holds the matching mTLS
  client bundle. From another machine it reports the checks it cannot
  evaluate as SKIP rather than as failures.
- The verifier's two *negative* tests — the credential placeholder being
  refused on a policy-allowed host it is not bound to, and the agent being
  refused when pointed at another base URL — are stronger than the plan
  requires and were **not** confirmed by an automated run. They are the
  difference between "routing is configured" and "routing is enforced by
  the runtime", so they become the first thing to confirm in Phase 2.
- GitHub Actions CI is still missing (B2 said "if time allows"; the lint
  and test configuration exists, the workflow does not).
- Two models named in planning do not exist on Token Factory
  (`nvidia/NVIDIA-Nemotron-3-Nano-Omni`, `Qwen/Qwen2.5-VL-72B-Instruct`).
  The OCR model was re-chosen from the live catalog — see the runbook.

---

## Phase 1 — Core Pipeline (Oct 21–23)

**Goal:** Working end-to-end agent loop: document in → deadline flagged → draft action out.

### Person B (lead)
- [x] Build document/email ingestion (parsing, chunking, extraction using the OCR/vision model from Phase 0) — `agent/ingestion/`; local extraction is primary, the vision model is the fallback (`parse_documents.py`)
- [x] Build persistent memory layer in Hermes: household picture, what's already been handled — `agent/memory/hermes_memory.py`
- [x] Build deadline-flagging logic from extracted document data — `agent/deadlines/flag_deadlines.py`, line-level evidence for every date
- [x] Build first draft-action generation (e.g. "draft cancellation email," "draft refund request") — `agent/actions/draft_actions.py`

### Person A
- [x] Wire Hermes/OpenShell so B's pipeline runs on the real sandbox from day one, not bolted on later — Ledger's packages ship in the image at `/opt/ledger` and are launched past Hermes's own `agent` package; the live-sandbox run is the last §5 acceptance check
- [x] Start scaffolding egress policy rules in OpenShell (what's allowed to leave the sandbox, to where) — not enforcing yet, just structure — `sandbox/policy.yaml`
- [x] Review B's pipeline for anywhere sensitive data might leak outside the sandbox unguarded — nine surfaces reviewed; F-038 and F-039 found and fixed

**Exit condition:** Feed a document in, get a flagged deadline + a draft action out the other side, running inside the sandbox.

---

## Phase 2 — Privacy Router (Oct 24–25)

**Goal:** Build the project's actual technical differentiator.

### Person A (lead)
- [x] Implement Nemotron Nano redaction layer — strips personal details before data reaches Super/Ultra models or Tavily — `privacy_router/redact.py`: the deterministic pass plus the Nano escalation. Measured at **0/200** outbound payloads carrying a personal detail (`docs/leak-test.md`).
- [x] Wrap it in an OpenShell supervisor middleware — `sandbox/middleware/ledger-router/`: `Describe`/`ValidateConfig`/`EvaluateHttpRequest`, JSON-aware body redaction, image detection, the ALLOW/REDACT/DENY guard (`privacy_router/injection_guard.py`), and a fail-closed audit JSONL. The service is tested with a real local gRPC run and `probe.py --self-test`; live gateway registration is the remaining confirmation.
- [x] Implement OpenShell egress policy enforcement (block/allow rules on outbound traffic) — the base rules live in `sandbox/policy.yaml`; `sandbox/egress_probe.sh` proves allowed host/path, unknown host, wrong path and wrong port from **inside** the sandbox. The router-enabled policy is generated by `sandbox/router_policy.py`, never by editing the safe base.
- [x] Build the live prompt-injection-block demo scenario — `demo/injection/`: a synthetic untrusted message, an in-sandbox attempt script, and a runner that shows the network-policy denial, the two middleware denials, the benign control, and the audit rows. `sandbox/verify_phase2.sh` runs the whole exit condition.

**Status (2026-10-24).** The Phase 2 code and operator tooling are
implemented and pass the offline suite: middleware, guard, OCSF audit
normaliser, policy/gateway generators, egress probe, registration scripts,
demo, and verifier. The live gateway run is **blocked on this machine** by
the same client-certificate condition recorded in F-030 (`openshell status`
→ `invalid peer certificate: BadSignature`), a missing Docker daemon, and
`host.openshell.internal` not resolving; `sandbox/verify.sh` reports 2
required failures and 9 skips. The live procedure and its failure modes are
in `docs/phase2-runbook.md`. The exit condition is not closed until
`bash sandbox/verify_phase2.sh` prints `PASS` on the gateway host.

### Person B
- [x] Build the leak-test harness: run the ~200 synthetic docs (from Phase 0) through the pipeline with the router ON and OFF — `eval/leak_test.py`; every outbound payload is captured at one seam (`privacy_router/egress.py`), so the run needs no network and no API key
- [x] Measure and log how often sensitive details appear in outbound queries in each condition — **200/200 (100%) OFF, 0/200 (0%) ON**, raw numbers in `eval/results/leak_test.json`
- [x] Package results into a clear before/after comparison (table or chart) for the video — `docs/leak-test.md` and the README table; the three line items are broken out

**Exit condition:** Real leak-rate number (with/without router), injection block reproducible on screen with audit log.

---

## Phase 3 — Evaluation (Oct 26–27)

**Goal:** Turn the three proofs into reportable numbers for the video.

### Person B
- [x] Build memory-with-updates eval: test questions where underlying facts changed (e.g. "I cancelled that plan in March")
  - `make memory-eval` — 30 isolated scenarios across 8 categories (status, amount, renewal_date, holder, contact, preference, confidence, restatement). The `never_stated` type queries the store instead of short-circuiting to "", so abstention is actually tested. `eval/scenarios/memory_updates.json`, `tests/test_memory_updates_eval.py`.
- [x] Score accuracy, since most demos only test simple recall — this is meant to stand out
  - Ledger **100.0%** overall (30/30) against an update-blind baseline of **43.3%**; on current facts after a supersession the baseline gets **15.8%**. Per-category and per-answer-type tables in `docs/phase3-runbook.md` §3. Deterministic SQLite supersession, no model call.

### Person A
- [ ] Verify Nebius Serverless Jobs background deadline monitoring is actually running live (not just scheduled)
  - The job is implemented and tested (`jobs/deadline_monitor.py`, `tests/test_deadline_monitor.py`), with `--status` as the liveness check and a run record as the evidence. **Live verification is blocked on this host:** no `nebius` CLI, no service-account credentials, and no Serverless Job spec. The checkbox stays open until `--status` exits 0 on a host where the schedule runs — see `docs/phase3-runbook.md` §2.
- [x] Benchmark cost and latency per Nemotron tier (Nano / Super / Ultra) across a representative set of calls
  - `make bench` — 60 measured calls over four request shapes, live catalog model IDs and live pricing, `eval/results/cost_latency.json` and `docs/phase3-runbook.md` §1. Headline: Nano is cheapest and *slowest* (p95 18.4 s vs 6.9 s for Super); Super costs ~6.9× Nano per call because of its reasoning trace.

### Joint
- [x] Compile the three results (leak rate, memory accuracy, cost/latency table) into video-ready visuals
  - `make results` compiles `docs/results.md` and four SVGs from the artifacts, and refuses to draw a number whose artifact is missing or a smoke run. All three are READY: leak 100% → 0% on 200 documents, memory 100% vs 43.3% baseline on 30 scenarios, cost/latency across the three tiers.

**Exit condition:** 3 numbers/charts ready to drop into the video.

---

## Phase 4 — Packaging (Oct 28)

**Goal:** Make the project submittable by a stranger.

### Person B
- [x] Polish the chat interface (Telegram/Slack) and simple web view — `interface/bot/handlers.py`, `interface/web/server.py`, `make demo-web`; 35 interface tests
- [x] Test the demo fully logged-out, fix any auth-wall issues — local demo needs no login or credentials; no hosted URL by design (loopback-only, see `docs/demo.md`)

### Person A
- [x] Write README: highlight Nemotron + Token Factory usage, setup steps, architecture overview — `README.md`
- [x] Write up the friction log into the feedback section required by the rules — `docs/feedback.md` (full record in `docs/friction_log.md`)
- [x] Add open-source license to repo — Apache-2.0, `LICENSE`

### Joint
- [x] Cross-review each other's output before EOD — `docs/packaging-review.md` (8 interface findings, 6 docs findings, all fixed)

**Exit condition:** A stranger could open the repo and demo URL and understand it in 5 minutes.
**Status:** met locally (`make demo-web`); no public demo URL exists because the
server is loopback-only and the household database has no authentication.
See `docs/demo.md` and the residual risks in `docs/packaging-review.md`.

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
│   ├── gateway.toml               # base gateway config (registration commented)
│   ├── policy.yaml                # base egress policy (middleware commented)
│   ├── router_policy.py           # generate the router-enabled policy
│   ├── gateway_config.py          # toggle the gateway registration
│   ├── router_on.sh / router_off.sh
│   ├── router_register.sh / router_unregister.sh
│   ├── egress_probe.sh            # host/path/port proof from inside
│   ├── verify_phase2.sh           # Phase 2 exit verification
│   ├── audit_tail.py              # safe decision printer
│   ├── middleware/ledger-router/  # the gRPC service + generated protos
│   └── setup.sh                   # Hermes + Token Factory routing setup (Phase 0)
│
├── privacy_router/                # Phase 2 core differentiator (Person A)
│   ├── redact.py                  # Nemotron Nano redaction layer
│   ├── injection_guard.py         # Live prompt-injection block logic
│   └── audit_log.py               # Audit trail + OCSF normaliser
│
├── demo/injection/                # the on-screen injection scenario
│   ├── untrusted_message.txt
│   ├── household_record.json
│   ├── attempt_exfil.py
│   └── run_injection_demo.sh
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
│   ├── phase2-runbook.md          # live middleware procedure + failure modes
│   └── architecture.md            # Diagram + explanation for README/video
│
└── video/
    └── script.md                  # Phase 5 narration script (Joint)
```

**Ownership at a glance:** `sandbox/`, `privacy_router/`, `jobs/`, `search/` → Person A. `agent/`, `interface/` → Person B. `eval/` and `docs/` are split per-file as noted above but live together so both people can see proof status at a glance.

---

## Key dependency note

Phase 1 → Phase 2 is the one hard dependency: the router needs a working pipeline to route. The Phase 0 joint scouting (OCR models + synthetic docs) is designed to absorb that risk early so Phase 2 doesn't start from zero.

---

# Upgrade Phase — Glass-box multi-agent pipeline (Oct 29)

**Track:** Personal AI — Nebius × NVIDIA Global AI Hackathon
**Hard deadline:** Oct 30, 2026, 10:00 AM PDT (local time: `__________`)
**Relationship to the plan above:** additive. Phases 0–5 stay as built;
nothing in them is replaced.

> **Status at integration (2026-10-29).** This section is the plan plus an
> honest gap analysis, not a claim that the upgrade is built.
>
> | Area | Status |
> |---|---|
> | Phase 0 — safety net | **partial** — Token Factory run and secret scan done; repo is private, licence is Apache-2.0 (not MIT), no spending cap |
> | Phase A — glass-box trace | **not started** — no trace module or timeline UI |
> | Phase B — roles and Auditor | **not started** — no orchestrator, role contracts, or planted cases |
> | Phase C — Verifier/dashboard | **not started** — the existing deadline job computes days-until, but there is no Verifier or findings dashboard |
> | Phase D — four evals | **3 of 4 measured** — leak, memory and tier cost/latency exist; Auditor catch rate does not |
> | Phase E — submission package | **partial** — README, feedback and local demo exist; no public demo URL, video, or Devpost submission |
>
> **Checked boxes below mean "already true in this repository", with the
> evidence path given. Unchecked boxes are open work.** The upgrade is not
> reflected in the code yet; do not describe it as built in the video or
> README until the corresponding box is checked.

## Upgrade 0 — Goal

Ledger currently feels like a chatbot with an agent behind it. This plan
makes the work **visible and verifiable**:

1. A **glass-box trace panel** showing every step, model tier, latency, and
   what left the sandbox.
2. A **multi-agent pipeline** with a justified role split, including an
   **Auditor** that can reject a draft.
3. A **code-based Verifier** so facts (totals, dates, duplicates) are
   computed, not guessed.
4. A **proactive-first demo** (the background job pushes findings; chat is
   secondary).
5. **Measured results** (leak test, memory-with-updates, tier cost/latency,
   Auditor catch rate).

## Upgrade 1 — Time rules

| Milestone | Latest time |
|---|---|
| Feature freeze (no new features after this) | 6 h before deadline |
| Video recorded (plus a backup take) | 4 h before deadline |
| Submitted on Devpost | 2 h before deadline |

- If a phase misses its cut line, **drop it and list it as roadmap in the
  README**. Do not risk the core demo.
- Tag the current working state in git as `pre-upgrade` before touching
  anything. *(`pre-upgrade` is not tagged yet; the pre-upgrade state is
  `8c262e4` on `main`/`agents`.)*

## Upgrade 2 — Target architecture

```
Trigger: user upload  OR  Watcher (Nebius Serverless Job: deadlines, follow-ups)
                    │
                    ▼
        ORCHESTRATOR  (deterministic code, no LLM)
        routes steps, holds state, enforces retry cap
                    │
   ┌────────────────┼───────────────────────────────┐
   ▼                ▼               ▼               ▼
Redactor        Extractor       Verifier        Researcher
(Nano)          (Nano)          (plain code)    (Super + Tavily,
strips PII      amounts, dates, sums, dates,    redacted queries
FIRST           vendors         duplicates      only)
                    │
                    ▼
                 Drafter (Super)
                    │
                    ▼
        AUDITOR (Ultra) — reviewer with veto, not a supervisor
            │ reject + reasons ──► Drafter (max 2 rounds)
            ▼ approve
      Human approval ──► user saves or sends (never auto-send)
```

All model calls go to **Nebius Token Factory** using **Nemotron** models.
Everything runs under the **OpenShell** policy. **Hermes** keeps memory,
skills, and scheduling.

### Design rules

1. Redactor always runs first. Nothing leaves the sandbox before it.
2. The Verifier is code. Its output is ground truth for the Auditor.
3. The Auditor only approves or rejects with reasons. It takes no actions.
4. Max 2 rejections, then status `needs_human_review`. No infinite loops.
5. Every handoff is logged to the trace (Phase A).
6. The user approves every outbound action.

## Upgrade 3 — Framework decision (time-boxed)

Recommended: **LangGraph** (fixed graph with a conditional Auditor edge).
Fallbacks: **plain Python functions** (same structure) or **CrewAI Flows**.
Skip AutoGen (maintenance mode).

**Gate (60 minutes max):**

- [ ] Build a 3-node toy graph: Drafter → Auditor → conditional edge (back
      to Drafter or End), calling a Token Factory Nemotron model.
- [ ] If it works within 60 minutes → use LangGraph for the pipeline.
- [ ] If not → plain Python with the same node/edge structure. Do not keep
      fighting the framework.

*Status: not started. No LangGraph/CrewAI dependency is installed, and no
multi-agent graph exists. The existing pipeline is deterministic plain
Python, which is a valid fallback shape but is not this graph yet.*

**Framework hygiene (before recording the video):**

- [x] All model calls point at Token Factory (no default-provider fallback).
      *Existing benchmarks and OCR resolve through the pinned catalog in
      `eval/tokenfactory.py`; `DEFAULT_BASE_URL` is
      `https://api.tokenfactory.nebius.com/v1` and no alternate provider is
      configured. Re-verify after the graph lands.*
- [x] Third-party tracing/telemetry disabled. *No LangSmith, CrewAI or
      AutoGen packages are declared in `pyproject.toml`; logs stay local.
      Re-check when a framework is added.*
- [x] Hermes stays in place for memory, skills, scheduling.
      *`agent/memory/hermes_memory.py`, the messaging gateway and
      `jobs/deadline_monitor.py` are the plan of record.*

## Upgrade 4 — Phases

### Upgrade Phase 0 — Safety net (30 min)

- [ ] Tag `pre-upgrade` in git. *No tag exists; the pre-upgrade commit is
      `8c262e4`.*
- [x] Confirm one end-to-end run works on Token Factory with a Nemotron
      model. *`eval/results/cost_latency.json`, measured 2026-10-28: 60
      calls, 0 errors across Nano, Super and Ultra; the Phase 0 run also
      returned `LEDGER-OK` from inside the sandbox (see the Phase 0 status
      above).*
- [ ] Repo is public, `LICENSE` (MIT) at the repo root, shows in the GitHub
      sidebar. *The repo is currently **private** and the licence is
      **Apache-2.0**, not MIT (`gh repo view` reports `visibility: PRIVATE`,
      `licenseInfo.key: apache-2.0`).*
- [x] Secret scan of code **and git history**. Rotate any key that was ever
      committed. *`.env` has never been tracked; a targeted scan of all
      commits for `nvapi-`, `xoxb-`, `xoxp-`, `tvly-`, `ghp_`, `AKIA`,
      `sk-proj-` and `sk-ant-` found no live credential. No rotation was
      needed. Run a dedicated scanner (for example `gitleaks`) before the
      repository is made public.*
- [ ] Set a spending cap and rate limit on the demo keys. *Not configured in
      the repo and not verifiable from it.*

**Done when:** one run works, repo is public with licence, no secrets found.
*Two of three are true; the repo is not public and the licence is not MIT.*

### Upgrade Phase A — Glass-box trace panel (about 3 h) — REQUIRED

**Event schema** (one JSON line per step, also kept in memory for the UI):

```json
{
  "run_id": "...", "step": 3, "agent": "Auditor",
  "model": "<nemotron variant>", "tier": "ultra",
  "latency_ms": 0, "tokens_in": 0, "tokens_out": 0,
  "input_summary": "<redacted summary, no raw PII>",
  "output_summary": "<short>",
  "egress": "none | allowed:<host> | blocked:<host>",
  "status": "ok | rejected | needs_human_review | error"
}
```

**UI:** a timeline view (one row per step) that updates live, with a badge
for the tier and a red/green badge for egress.

- [ ] Log events from every existing stage.
- [ ] Timeline panel in the web view.
- [ ] Confirm the trace never shows raw personal data.

*Status: not started. There is no trace module, event schema, or timeline
panel. The audit JSONL in `privacy_router/audit_log.py` records security
decisions for outbound requests; it is not a per-run agent trace and does
not feed the UI.*

**Done when:** a run shows 5+ steps with tier, latency, and egress status,
readable in a screenshot.
**Cut line:** none. This is the main fix for the "wrapper" feel.

### Upgrade Phase B — Agent roles and Auditor (about 4 h)

Shared state passed between nodes:
`{ doc_id, redacted_text, extracted_fields, verified_facts, research_notes, draft, audit_result, retries, status }`

**Role contracts**

| Role | Input | Output |
|---|---|---|
| Redactor (Nano) | raw text | redacted text + redaction map (kept inside sandbox) |
| Extractor (Nano) | redacted text | fields: vendor, amounts, dates, line items |
| Verifier (code) | fields | facts: totals match?, duplicates, days left to deadline |
| Researcher (Super + Tavily) | vendor + issue type | rule/policy notes with source URLs (redacted queries only) |
| Drafter (Super) | verified facts + research notes | letter or reminder with citations |
| Auditor (Ultra) | draft + source spans + verified facts | `{decision, reasons[], uncited_claims[]}` |

**Auditor checks**

1. Every factual claim cites a source span or a Verifier fact.
2. Amounts and dates in the draft match Verifier output exactly.
3. No personal data beyond what the user's own letter needs.
4. No promised outcomes ("you will get a refund") and no legal-advice
   phrasing.

**Planted-error test cases** (synthetic, saved in `evals/auditor_cases/`):

- [ ] Case 1: draft states a wrong amount.
- [ ] Case 2: draft contains an uncited claim.
- [ ] Case 3: draft promises an outcome.

*Status: not started. The existing pipeline has a deterministic redactor,
a local extractor and template-based drafter (`privacy_router/redact.py`,
`agent/ingestion/`, `agent/actions/draft_actions.py`), but there is no
orchestrator, shared state object, Researcher, or Auditor. No
`evals/auditor_cases/` directory exists.*

**Done when:** the Auditor rejects all 3 cases, the Drafter revises, the
second pass is approved, and both rounds show in the trace.
**Cut line:** if the framework gate fails, use plain Python. Do not drop the
Auditor.

### Upgrade Phase C — Verifier and proactive dashboard (about 2–3 h)

**Verifier checks (plain code):**

- [ ] Line items sum to the stated total.
- [ ] Duplicate charge detection (same vendor, amount, near dates).
- [ ] Days remaining to a deadline or refund window.
- [ ] Upcoming-expiry flag (for example within 14 days).

**Proactive-first demo:**

- [ ] The Nebius Serverless Job (Watcher) runs the Verifier on stored
      documents and pushes a summary ("2 deadlines this week, 1 likely
      duplicate charge").
- [ ] The web view opens on a **findings dashboard**; chat is secondary.

*Status: not started as specified. `jobs/deadline_monitor.py` already
computes `days_until` and notifies on upcoming deadlines (tested in
`tests/test_deadline_monitor.py`), so the days-remaining logic has a
foundation. It does not run a Verifier, detect duplicate charges, or push a
findings summary, and the web view still opens on the household list. Live
scheduler verification is also still blocked (see Phase 3).*

**Done when:** the demo starts from a pushed finding, not a typed prompt.
**Cut line:** skip dashboard polish; keep a plain list of findings.

### Upgrade Phase D — Evals with real numbers (about 2–3 h)

All data is **synthetic**. Say so everywhere the numbers appear.

| Eval | Size (minimum) | Metric | Status |
|---|---|---|---|
| Leak test | 50 docs with fake PII | Sensitive items found in outbound queries, with and without the router | **done — 200 docs** |
| Memory with updates | 20 questions, including changed facts | Accuracy (for example "cancelled in March") | **done — 30 scenarios** |
| Auditor catch rate | the 3 planted cases + any extras | Rejected / total, false-approve count | **not measured** |
| Tier cost and latency | one table | Avg latency and token use per tier (Nano, Super, Ultra) | **done — 60 calls** |

- [x] Save scripts in `evals/` and results in `evals/results.md`.
      *Existing layout is `eval/` (scripts) plus `docs/results.md` and the
      committed JSON in `eval/results/`. The substance is present; the
      directory name and output path differ from this plan.*
- [x] Put the headline numbers in the README. *Leak 100% → 0%, memory
      100% vs a 43.3% baseline, and the tier table are in the README and
      `docs/results.md`.*

*Status: three of the four metrics exist at or above the minimum sample
size. There is no `evals/results.md` and no Auditor catch-rate table, so the
phase's "Done when" is **not met**. Do not present the upgrade as having
four measured numbers.*

**Done when:** `evals/results.md` exists with all four tables.
**Cut line:** reduce sizes (even 25 docs is acceptable) but keep all four
metrics. Report the true sample size.

### Upgrade Phase E — Submission package (about 2–3 h, before the freeze)

**Repo**

- [x] README: problem, architecture diagram, Nemotron and Token Factory use
      called out clearly, eval numbers, how to run, known limits.
      *`README.md` covers the problem, stack, three claims, setup, demo and
      limits; the full diagram is in `docs/architecture.md`.*
- [ ] `LICENSE` (MIT) visible at root; no secrets in code or history.
      *A root `LICENSE` exists and is visible, but it is Apache-2.0, not
      MIT. No secrets were found (see the Phase 0 scan).*
- [ ] Short "What was updated during the hackathon" note if the project
      pre-existed. *Not written.*

**Demo URL**

- [ ] Works without login. *There is no hosted URL. The local
      `make demo-web` view has no login and needs no credentials, but it is
      loopback-only by design; see `docs/demo.md`.*
- [ ] Banner: "Synthetic documents only. Do not upload real personal data."
      *The local header says "Demo · fictional data"; the required banner is
      not present.*
- [ ] Uploads auto-delete; rate limit and size cap on; spending cap on keys.
      *Demo uploads are written to a temporary file and deleted, the demo
      database is temporary, and the upload cap is 15 MiB. There is no rate
      limit and no spending cap.*
- [ ] Notice: outputs are information, not legal or financial advice. *Not
      present.*

**Devpost form**

- [ ] Track: Personal AI. *Intended track; no submission yet.*
- [ ] Project description, demo URL, public YouTube video (under 3
      minutes), repo link. *Not submitted; no video exists.*
- [x] Written feedback on the Nebius and NVIDIA tools (from your friction
      log). *`docs/feedback.md`; the full record is
      `docs/friction_log.md`.*
- [ ] City Winner option only if you attended an eligible in-person event.
      *N/A unless attendance is confirmed.*

## Upgrade 5 — Demo and video script (under 3:00)

| Time | Show |
|---|---|
| 0:00–0:25 | Problem in plain words; the promise: remembers everything, can't be tricked |
| 0:25–1:05 | Watcher pushes a finding; dashboard shows deadline + likely duplicate charge |
| 1:05–1:50 | Trace panel: Redactor → Extractor → Verifier → Researcher → Drafter → **Auditor rejects a bad draft** → fixed → approved |
| 1:50–2:25 | Privacy: raw query vs redacted query that left; injection attempt blocked, audit log visible |
| 2:25–2:50 | Numbers: leak test, memory-with-updates, tier latency and cost (labelled synthetic) |
| 2:50–3:00 | Stack on Nebius + Nemotron; close |

Record a backup take. Keep one fully pre-seeded run ready in case the live
service fails.

*Status: no script or recording exists yet. The current product walkthrough
and the privacy/cost evidence in `docs/results.md` can supply the 1:50–2:50
segment, but the trace/Auditor segment depends on Phases A and B.*

## Upgrade 6 — Honesty and legal guardrails

- Describe the architecture exactly as built (for example "LangGraph
  pipeline" or "plain-Python roles"); do not claim a framework that isn't in
  the code.
- Privacy claim wording: "minimal data to the large model, redacted outbound
  queries, policy-enforced egress." Do not claim "zero data leaves" or
  "zero-retention" unless verified.
- Label all measured results as synthetic test-set results, not general
  guarantees.
- No real email accounts, vendor logins, or scraping. Uploaded or pasted
  documents only.
- The agent drafts; the user approves and sends. No outcome promises.
- Check dependency and model licences against the chosen licence.

*Current compliance:* the README already scopes the leak claim to the
router's coverage rather than sandbox enforcement, labels the corpus as
synthetic, and states that Ledger never sends. The licence mismatch in
Upgrade Phase E is the open item.

## Upgrade 7 — Risks and fallbacks

| Risk | Fallback |
|---|---|
| Framework install or behaviour issues | Plain Python roles with the same state object |
| Hermes/OpenShell integration flakiness | Freeze the last working configuration; do not experiment after Phase A |
| Auditor too strict or too lenient | Fix the 3 planted cases first; tune only if time remains |
| Live demo network failure | Pre-seeded run and recorded backup video |
| Eval takes too long | Smaller sample, report the true size |
| Time overrun | Drop Phase C dashboard polish, then reduce Phase D sizes; never drop Phase A, the Auditor, or the video |

## Upgrade 8 — Out of scope (do NOT start)

- New modules or features (calibration coach, new integrations).
- Real email OAuth, vendor account automation, WhatsApp integration.
- Any refactor not required by Phases A–E.

## Upgrade 9 — Final checklist before pressing Submit

- [ ] Tagged commit pushed; repo public; licence visible. *No tag; repo
      private; Apache-2.0 licence visible.*
- [ ] Demo URL tested from a private/incognito window without login. *No
      hosted URL.*
- [ ] Video public on YouTube, under 3 minutes, plays correctly. *Not
      recorded.*
- [ ] README numbers match `evals/results.md`. *There is no
      `evals/results.md`; the README numbers do match `docs/results.md` and
      the committed artifacts.*
- [x] No secrets in repo or history; keys rotated if ever exposed. *Targeted
      scan clean; `.env` never tracked; nothing to rotate.*
- [ ] Track selected: Personal AI. *Intended; form not submitted.*
- [x] Feedback section filled. *`docs/feedback.md`.*
- [ ] Submitted at least 2 hours before the deadline. *Not submitted.*

---

## Upgrade integration note

If this upgrade is implemented, the first three files to add are an
orchestrator/state module, a trace writer that feeds the web timeline, and a
code-based Verifier. The existing `eval/` artifacts already satisfy three
of the four Phase D metrics; only the Auditor catch rate requires the new
pipeline. Nothing in Phases 0–5 needs to be removed to make room for it.
