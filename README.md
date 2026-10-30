# Ledger

**A privacy-routed personal agent for deadlines, renewals, and entitlements.**

Ledger reads the documents that come at you — insurance policies, gym
memberships, subscriptions, licence renewals, medical renewals — notices
what is about to lapse, tells you, and drafts the action. It runs inside a
sandbox with a declarative egress policy, and a privacy router strips your
personal details from anything that leaves, so the expensive models and
third-party search engines never see them.

> **Status: Phases 0–4 plus the glass-box upgrade built; live gateway and serverless-job proofs pending.**
> The stack is de-risked and the decisions are recorded. The Phase 0 exit
> condition was met by hand on 2026-10-21; the automated verifier has not
> yet produced a green run, and two Phase 0 benchmark runs disagree about
> which model reads documents (F-032). The Phase 1 pipeline runs end to
> end, but the sandbox leg of its exit condition is outstanding. The Phase 2
> middleware, guard, audit trail and injection demo are implemented and
> pass the offline suite; the live gateway leg is blocked by the client
> certificate and missing Docker daemon recorded in F-030/F-043. Phase 3
> has all four numbers — leak 100% → 0%, memory 100% vs a 43.3% baseline,
> the per-tier cost/latency table, and a 5/5 Auditor catch rate on planted
> synthetic errors — but the Nebius Serverless Job is implemented, not yet
> observed firing on a scheduler. Phase 4 packages the result: a loopback
> web view with a fictional demo, the friction log distilled into
> [`docs/feedback.md`](docs/feedback.md), and a recorded cross-review in
> [`docs/packaging-review.md`](docs/packaging-review.md). The upgrade
> adds a glass-box trace, a **LangGraph role graph** (with a plain-Python
> fallback), a code Verifier, a rule-based Auditor with a bounded
> revision loop, a findings-first dashboard, and an opt-in Nemotron Super
> Drafter. The Auditor is deterministic code, not a Nemotron Ultra call:
> the model can draft, but it cannot approve its own work. See
> [`docs/phase0-runbook.md`](docs/phase0-runbook.md) for the infra side,
> [`docs/ocr-decision.md`](docs/ocr-decision.md) for the model decision
> and the privacy architecture,
> [`docs/leak-test.md`](docs/leak-test.md) for the leak measurement,
> [`docs/phase2-runbook.md`](docs/phase2-runbook.md) for the live router
> procedure, [`docs/phase3-runbook.md`](docs/phase3-runbook.md) for the
> evaluation numbers and the deadline job,
> [`docs/demo.md`](docs/demo.md) for the five-minute tour, and
> [`IMPLEMENTATION.md`](IMPLEMENTATION.md) for the full plan.

## Try it in five minutes

```bash
make demo-web
# Ledger local interface: http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765> for the Ledger overview, then choose **Open Ledger** to enter the dashboard at `/app`. The dashboard header says **Demo · fictional data**.
The demo uses a temporary database seeded with six invented items, so it
never reads or writes the real household sidecar. It opens on a pushed
finding — two deadlines this week and one likely duplicate charge — and
shows the glass-box trace: Redactor → Extractor → Verifier → Researcher →
Drafter → **Auditor rejects a wrong amount** → revision → approved. Cancel
an item, watch it leave *Coming up*, reactivate it, upload a notice, and
review the draft Ledger prepared without sending.

```bash
make demo-web   # fictional data, temporary database, loopback only
make web        # the real household database, loopback only
```

The interface binds to `127.0.0.1`, refuses non-loopback `Host` headers and
rejects cross-origin requests. The household database is never hosted.

A separate **public demo** runs the same handler on Vercel
(`app.py`, `interface/web/wsgi.py`, `vercel.json`) against a throwaway
database seeded with the same fictional items. It never reads the
household sidecar, makes no model calls, and resets when the instance
recycles. The tour, including what the demo does and does not prove, is in
[`docs/demo.md`](docs/demo.md).

### Chat: Telegram

The Telegram adapter runs the same `LedgerBot` and the same household
database as the web view, so chat and browser see the same state.

```bash
make telegram-bot   # long polling, no public URL or webhook
```

`TELEGRAM_BOT_TOKEN` is the only required value. `TELEGRAM_CHAT_ID` is only
for proactive notices from the deadline monitor. The adapter accepts a text
command or a PDF/text notice dropped into the chat:

* `/due` — what is coming up in the next 30 days
* `/cancel ITEM_ID [YYYY-MM-DD]` and `/active ITEM_ID [YYYY-MM-DD]`
* `help` / `/start` — the command list
* natural questions such as “When does Orchard Fitness renew?”

An attachment is downloaded to a temporary file, parsed locally, stored in
the household memory, and deleted. The reply is a status line plus a
redacted draft preview; the full draft stays in the loopback web view.
Ledger never sends a letter from chat.

Proactive deadline notices are separate: `jobs/deadline_monitor.py` posts
to `TELEGRAM_CHAT_ID`. A Slack incoming webhook remains a one-way
notification option; there is no interactive Slack bot.

Setup, privacy boundary and the honest live status are in
[`docs/telegram.md`](docs/telegram.md).

### Phase 0 decisions

| Decision | Outcome | Record |
|---|---|---|
| Which model reads documents? | `nvidia/Nemotron-3.5-Super-VL` — 99% field recovery, $0.30/1M in | [`docs/ocr-decision.md`](docs/ocr-decision.md) |
| How do personal details stay inside the sandbox? | Extract locally → redact locally → the model only ever reads redacted text | [`docs/ocr-decision.md`](docs/ocr-decision.md) |
| What does the deterministic redactor miss? | Names, alphanumeric identifiers, city and postcode — now closed: **200/200 documents clean** with the household's names | [`docs/redaction-audit.md`](docs/redaction-audit.md) |
| Which model IDs do we use? | Pinned from the live catalog, never hand-typed | [`docs/model-catalog.md`](docs/model-catalog.md) |
| Can the model read redacted text? | **20/20 documents, 100% on every field, zero hallucinations, and the model saw no personal detail at all** | [`docs/ocr-decision.md`](docs/ocr-decision.md) |

The last row matters more than it looks: Nebius's web catalog, its API
catalog and its cookbook disagree about which models exist, and the same
model is published under several casings. Every model ID in this repo
comes from the live catalog.

### Phase 1 — the pipeline

```
document ──► ingestion ──► memory ──► deadlines ──► action
             (local OCR,    (SQLite     (evidence      (a draft
              then local     sidecar)    for every      the user
              redaction)                 date)         sends)
```

```bash
make demo      # run it over the corpus and print what it produced
```

**The whole pipeline runs without a model call.** That is the design, not
a fallback: a renewal notice that says "your policy renews on 2027-03-07"
does not need a language model, and a pipeline that does not need one
cannot leak anything to one. The model is used where the text is prose
rather than a form, and even then it only ever sees redacted text.

The local extractor is **tesseract**, the same one the sandbox image
ships. On the corpus it recovers 100% of the fields and 100% of the
document's lines, at 1.3s a page, with no egress at all.

> A benchmark is only as representative as its fixtures. The corpus used
> to render at 96 DPI, where the digit `0` is indistinguishable from an
> `8` — tesseract read `2027-03-07` as `2827-83-07` and every number
> measured on it was wrong. It renders an A4 page at 150 DPI now, which
> is what a real scan gives you. See F-037 in the friction log.

### The glass-box upgrade — roles, Verifier, Auditor

The upgrade uses **LangGraph** for the role graph. The plan's gate was
time-boxed to 60 minutes; the graph compiled, ran, and called a real
Nemotron Super model, so the framework path is what the web view uses.
The plain-Python orchestrator is kept as the documented fallback and as
the reference implementation the tests compare against.

```
START ──► Redactor ──► Extractor ──► Verifier ──► Researcher ──► Drafter
            (local)      (local)       (code)       (optional)     │
                                                                   ▼
                                                               Auditor (code)
                                                                   │
                                    reject ──► Drafter (max 2) ◄───┤
                                                                   │ approve
                                                                   ▼
                                                                  END
```

* **Graph** (`agent/graph.py`) is a `StateGraph` with the shared state and
  a conditional edge from the Auditor back to the Drafter. The revision
  is deterministic, so a second round cannot introduce a new
  hallucination.
* **Fallback** (`agent/orchestrator.py`) is plain Python with the same
  state object and edges, for when the framework is unavailable.
* **Model client** (`agent/model.py`) calls Token Factory only. It
  resolves `LEDGER_MODEL_*` IDs pinned by `make pin-models` and raises if
  the key or ID is missing — there is no fallback provider.
* **Drafter** is a template by default. `LEDGER_USE_MODEL_DRAFTER=1` (or
  `--model-drafter` on the CLI) asks Nemotron Super for the letter using
  the **redacted** notice; the real reference is restored from the
  redaction map inside the sandbox. The deterministic Auditor then checks
  the model's output exactly as it checks the template.
* **Verifier** (`agent/verifier.py`) is arithmetic and date subtraction:
  line items sum to the total, duplicate charges (same vendor and amount,
  nearby dates), days remaining, and a 14-day expiry flag.
* **Auditor** (`agent/auditor.py`) is deterministic code with a veto. It
  rejects a draft whose amount or date disagrees with the Verifier, whose
  factual assertion has no Verifier fact or source span, that carries
  personal detail the letter does not need, or that promises an outcome
  or uses legal-advice phrasing. It takes no other action. Two rejections
  is the cap; then the status is `needs_human_review`.
* **Trace** (`agent/trace.py`) writes one JSON line per step to memory and
  optionally to a JSONL file. Summaries are redacted before they are
  stored, and egress is recorded as `none`, `allowed:<host>` or
  `blocked:<host>`. The web view reads the same events. LangSmith tracing
  is disabled explicitly; `langsmith` is only a transitive dependency.

The user still approves every outbound action: `RunState.status == "ok"`
means an approved draft is ready for a human, not that anything was sent.

The demo's trace is a real run over fictional data with a planted wrong
amount, so the rejection and the revision are visible in the UI. The same
path is measured by `make auditor-eval`, and the framework gate by
`make langgraph-gate`:

```bash
make auditor-eval     # 5 planted errors, catch rate, false approves
make langgraph-gate   # compile the graph + one real Nemotron Super call
make results          # recompile docs/results.md with all four numbers
```

### Phase 2 — the leak test

The agent has to look things up: *"what is the cancellation notice period
for this provider?"* The naive query quotes the reference and the holder,
because that is what makes a search specific — and those are exactly the
details that must not leave.

```bash
make leak-test    # 200 documents, router ON vs OFF, ~2 seconds, no network
```

| Condition | Outbound payloads | With personal detail | Leak rate |
|---|---:|---:|---:|
| router OFF | 200 | 200 | **100.0%** |
| router ON | 200 | 0 | **0.0%** |

Same pipeline, same documents, same query. The only difference is whether
the payload passes through `privacy_router.redact`:

```
OFF:  Lakeshore Family Health Plan medical cancellation policy 0847-4851 Devin Sullivan
ON:   Lakeshore Family Health Plan medical cancellation policy [ACCOUNT_1] [NAME_1]
```

Nothing is sent. Every outbound call crosses one seam
(`privacy_router/egress.py`), and the harness captures the payload there,
so the measurement costs nothing and is byte-identical on every run.

The router ON leg **can fail**: the test suite runs it with a router that
does nothing and asserts that it leaks (F-038, F-039). A 0% result that
cannot become 100% is not evidence.

> **The honest caveat.** This measures the redaction layer the OpenShell
> middleware invokes — the same `privacy_router.redact` — not the gateway
> applying it. The sandbox's ON condition is the presence of the
> `network_middlewares` entry, and that leg needs the gateway host. Until
> it runs, the claim is *"the router's coverage is 100%"*, not *"the
> sandbox enforced 100%"*.

See [`docs/leak-test.md`](docs/leak-test.md) for the method and what the
number does not cover.

### Phase 2 — the router in the path

The leak test measures the redaction function. The OpenShell supervisor
middleware is what makes the sandbox call it:

```
sandbox ──► network policy (host/path/port) ──► ledger-router ──► upstream
                                                ALLOW / REDACT / DENY
                                                + audit JSONL
```

It sees every allowed request to the hosts the policy attaches it to —
the inference endpoint and Tavily — including calls the agent code did not
write, and it returns one of three decisions with a stable reason code.
The guard denies an unknown host, a denied host, a task-host mismatch (when
`task_hosts` is configured), a third-party echo request (`include_answer`),
a bulk transfer of personal details, and an image going to a model not on
the OCR allowlist. The deterministic pass runs first; `escalate` defaults
to `false`.

```bash
make router-check                 # validate the config, no gateway needed
make router-serve                 # run the gRPC service on the host
make router-probe                 # Describe + redaction self-test
make router-register              # register with the gateway (probes first)
make router-on                    # attach the policy with --wait
make verify-phase2                # egress proof + audit + injection demo
make injection-demo               # the scenario on its own
make router-off                   # back to the base policy (the OFF leg)
```

The injection is deliberately persuasive: the agent reads
`demo/injection/untrusted_message.txt`, which tells it to upload the
household record to `collect.evil.example` and, as a fallback, to search
Tavily with `include_answer: true`. The script runs inside the sandbox and
makes all four requests. The unknown host is refused by the network policy;
the two Tavily shapes are refused by the middleware with
`third_party_model_echo_forbidden` and
`bulk_personal_detail_to_third_party`; the benign control is allowed. The
runner prints the new audit rows and refuses to print a log containing the
synthetic account or email.

The audit log stores the decision, the reason code, the host, a body hash
and **category counts** — never the request body or a personal value. If
the log is unwritable, the service fails closed. See
[`docs/phase2-runbook.md`](docs/phase2-runbook.md) for the exact live
procedure, rollback, and the current blocker.

---

## The claims and the four numbers

| # | Claim | Where it is proven | Phase |
|---|---|---|---|
| 1 | Personal details do not leak out of the agent | `eval/leak_test.py` — **100% → 0%** on 200 documents, see [`docs/leak-test.md`](docs/leak-test.md) | 2 |
| 2 | A prompt-injected agent cannot exfiltrate your data, and the attempt is on the record | `sandbox/middleware/ledger-router/`, `privacy_router/injection_guard.py`, `demo/injection/`, `sandbox/verify_phase2.sh` | 2 |
| 3 | The agent's memory is right when the facts change, not just when they are recalled | `eval/memory_updates_eval.py` — **100% on 30 scenarios** against a 43.3% update-blind baseline, see [`docs/results.md`](docs/results.md) | 3 |
| 4 | The Auditor rejects a bad draft before a human sees it | `eval/auditor_eval.py` — **5 of 5 planted errors** rejected, 0 false approves, 0 false rejects on the clean control | Upgrade |

Plus a cost/latency table across three model tiers
(`eval/cost_latency_bench.py`): **Nano is the cheapest and the slowest** —
p95 18.4 s against 6.9 s for Super — and Super costs ~6.9× Nano per call
because it reasons before it answers. The compiled numbers and charts are
in [`docs/results.md`](docs/results.md).

**All four numbers are synthetic test-set results**, not general
guarantees. The Auditor number is a deterministic check over five planted
error classes, not a claim that every real drafting mistake is caught.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Sandbox | [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) | kernel-level isolation, declarative egress policy, credential injection at the boundary, formally verified policy changes |
| Agent | [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research) | persistent household memory, self-created skills, messaging gateway on 20+ platforms, built-in Nebius Token Factory provider |
| Inference | [Nebius Token Factory](https://tokenfactory.nebius.com/) | OpenAI-compatible API, the Nemotron Nano / Super / Ultra tiers, vision models for OCR |
| Search | [Tavily](https://tavily.com/) | agent-oriented search and extract, routed through the privacy router |
| Privacy router | OpenShell supervisor middleware | sees **all** allowed egress, not just the calls we wrote |

---

## Setup

Full instructions live in [`docs/phase0-runbook.md`](docs/phase0-runbook.md).
The short version:

```bash
# 1. credentials
cp .env.example .env
$EDITOR .env                     # set NEBIUS_API_KEY

# 2. OpenShell + provider + image + sandbox + verification
bash sandbox/setup.sh

# 3. prove the Phase 0 exit condition (one pass/fail table)
bash sandbox/verify.sh

# 4. check that model calls really route to Token Factory
openshell policy get ledger-hermes --full
openshell logs ledger-hermes --tail
openshell term                   # live view: gateways, providers, sandboxes, blocked traffic
```

`bash sandbox/setup.sh --dry-run` exercises everything except the steps
that need a real key. `bash sandbox/verify.sh` is the artifact that proves
the Phase 0 exit condition — it is the file a reviewer runs.

> **Run `verify.sh` on the machine that hosts the OpenShell gateway.**
> The gateway requires mTLS, and the CLI reuses the local client bundle
> rather than fetching one from the gateway, so a client on another machine
> gets `invalid peer certificate: BadSignature` and cannot evaluate any
> of the checks. The script detects this and reports the dependent checks
> as SKIP rather than as failures, so a green-looking run from the wrong
> machine is impossible. To run it remotely, copy the gateway's bundle and
> register it under a name of your own:
>
> ```bash
> cp -a ~/.config/openshell/gateways/openshell/mtls <repo>/.openshell/
> # install it as ~/.config/openshell/gateways/<name>/mtls/, then:
> bash sandbox/verify.sh --gateway <name>
> ```

### Requirements

- Linux with Docker Engine 28.0+ (OpenShell's Docker compute driver)
- A Nebius Token Factory API key — <https://tokenfactory.nebius.com/>
- ~10 GB of disk (the Hermes image bakes in Chromium)

### On distributions without a `.deb` or `.rpm`

The upstream OpenShell installer supports Debian/Ubuntu packages, RPM
packages and the snap — and exits with *"Linux installs require either
dpkg or rpm"* on anything else, which includes Arch and CachyOS. The RPMs
are plain archives and `bsdtar` (libarchive) reads them, so
`sandbox/setup.sh` detects the case and installs from them itself. The
packages carry the CLI, the gateway, the prover, the systemd user unit and
the config-migration helper; the sandbox and supervisor container images are
pulled from `ghcr.io/nvidia/openshell/` on first sandbox creation.

### The models

| Purpose | Model | $/1M in/out |
|---|---|---|
| redaction, classification | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 0.06 / 0.24 |
| main agent reasoning **and** document OCR | `nvidia/Nemotron-3.5-Super-VL` | 0.30 / 0.90 |
| hardest reasoning | `nvidia/Nemotron-3-Ultra-550b-a55b` | 1.00 / 3.00 |

The Super tier is Super-VL: it is multimodal, it is the model the
decision record ([`docs/ocr-decision.md`](docs/ocr-decision.md)) names and
the Phase 2 privacy architecture was validated against. The choice was
measured, not preferred — 20 documents, five vision models, 99.0% field
recovery.

A second, independent run (30 rendered scans) preferred
`openbmb/MiniCPM-V-4_5` — 98.0% field recovery at $0.00050 per document
against Super-VL's 96.0% / $0.00124. That comparison is **open** (F-032):
re-deciding it means re-validating the privacy architecture against a
different reader, so it is the first item carried into Phase 1.

Both numbers were measured before the corpus was fixed (F-037), and have
been re-measured since. The current table, on the corrected corpus, is in
[`docs/ocr-decision.md`](docs/ocr-decision.md) — the short version is that
the **local** extractor now matches every vision model at 100% of fields
and 100% of lines, for no cost and with no egress at all. Two models named
in the original plan do not exist on Token Factory at all; see the
friction log. Reproduce with `make ocr-bench`.

---

## Repository layout

```
ledger/
├── README.md                     this file
├── IMPLEMENTATION.md             the phase plan this repo is built against
├── .env.example                  every variable, documented
├── pyproject.toml                deps, ruff, pytest
├── Makefile                      make help
│
├── sandbox/                      OpenShell config + policy + image
│   ├── gateway.toml              gateway configuration (schema v2)
│   ├── policy.yaml               sandbox egress policy
│   ├── providers/                provider profiles (credentials + endpoints)
│   ├── middleware/               Phase 2 — the privacy router service
│   ├── Dockerfile.hermes         the sandbox workload image
│   ├── docker-entrypoint.sh      the program the sandbox runs as its main process
│   ├── setup.sh                  idempotent setup; this is the README section
│   └── verify.sh                 the Phase 0 exit-condition verifier
│
├── agent/                        the pipeline and the role graph
│   ├── ingestion/                parse_documents.py, parse_email.py
│   ├── memory/                   hermes_memory.py, update_tracker.py
│   ├── deadlines/                flag_deadlines.py
│   ├── actions/                  draft_actions.py
│   ├── orchestrator.py           plain-Python fallback roles + RunState
│   ├── graph.py                  LangGraph StateGraph, conditional Auditor edge
│   ├── model.py                  Token Factory-only chat client
│   ├── verifier.py               totals, duplicates, days remaining
│   ├── auditor.py                deterministic veto + bounded revision
│   └── trace.py                  JSONL glass-box event log
│
├── privacy_router/               Phase 2 — redaction and guardrails
│   ├── redact.py                 the redaction library
│   ├── egress.py                 the seam every outbound request crosses
│   ├── injection_guard.py        exfiltration detection
│   └── audit_log.py              normalizes OpenShell's audit events
│
├── search/                       Tavily, routed through the router
├── jobs/                         Nebius Serverless Jobs — deadline monitoring
├── interface/                    chat handlers + loopback web view
│   ├── bot/handlers.py           a renewal message becomes an item
│   ├── chat/telegram.py          long polling -> LedgerBot -> reply
│   └── web/                      server.py, index.html, app.js
├── eval/                         the four proofs + the synthetic corpus
│   └── auditor_cases/            planted synthetic errors for the Auditor
└── docs/                         runbooks, friction log, feedback, architecture
```

Ownership: `sandbox/`, `privacy_router/`, `jobs/`, `search/` are Person A
(infra/privacy). `agent/` and `interface/` are Person B (agent/product).
`eval/` and `docs/` are shared.

---

## Development

```bash
make venv      # create .venv and install dev dependencies
make lint      # ruff
make test      # pytest
make help      # everything else
```

### The corpus and the benchmarks

```bash
make synth            # 200 documents + rendered PNGs, deterministic
make corpus-qa        # validate the corpus: coverage, ground truth, nothing clipped
make pin-models       # re-pin the live catalog into .env + docs/model-catalog.md
make ocr-bench        # which model reads documents best
make redacted-extract # can the model read redacted text? (the privacy architecture)
make redaction-audit  # what the deterministic redactor misses
make bench            # Nemotron tier cost/latency (needs NEBIUS_API_KEY)
make job-dry-run      # one deadline-monitor pass, no notifications
make auditor-eval     # Auditor catch rate over the planted cases
make results          # compile all four numbers into docs/results.md
```

`make synth` needs Pillow (`make venv` installs it). The corpus is
generated, not committed — it is reproducible from a fixed seed *and a
fixed reference date*, so the Phase 2 leak-test baseline stays
comparable across runs.

---

## Notes and caveats

- **The middleware contract is a research preview.** OpenShell's supervisor
  middleware API is explicitly marked as evolving. We pin OpenShell `v0.1.4`
  and keep the router behind a feature flag.
- **Public endpoints move.** Token Factory's global endpoint can change
  processing region without notice. For anything that needs a stable
  region, use a dedicated endpoint.
- **The agent's state lives in a named Docker volume, not a bind mount.**
  Hermes's WAL-mode SQLite can be silently corrupted by bind mounts that
  cross a VM boundary. See the friction log (F-008).
- **The upgrade path is LangGraph with a plain-Python fallback.** The
  framework gate passed: the graph compiles, runs the roles, and calls a
  real Nemotron Super model (`make langgraph-gate`). The Auditor and
  Verifier remain deterministic code — no model approves its own work —
  and third-party tracing is disabled.
- **The demo is loopback-only and there is no hosted URL.** Exposing it
  would put the household database behind no authentication. The repo is
  private and MIT-licensed; the public repo and spending-cap items are
  still open. POST endpoints are rate-limited (30 requests per 60 seconds
  per client by default, `LEDGER_RATE_LIMIT` / `LEDGER_RATE_WINDOW`), and
  the upload cap is 15 MiB.

---

## Licence

MIT — see [`LICENSE`](LICENSE).

## Feedback

The written submission is [`docs/feedback.md`](docs/feedback.md). The full
dated record is [`docs/friction_log.md`](docs/friction_log.md),
F-001 to F-050.

<!-- The friction log, in full, is required in the final submission. -->
