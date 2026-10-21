# Ledger

**A privacy-routed personal agent for deadlines, renewals, and entitlements.**

Ledger reads the documents that come at you — insurance policies, gym
memberships, subscriptions, licence renewals, medical renewals — notices
what is about to lapse, tells you, and drafts the action. It runs inside a
sandbox with a declarative egress policy, and a privacy router strips your
personal details from anything that leaves, so the expensive models and
third-party search engines never see them.

> **Status: Phase 0 — infra verification.**
> The stack is de-risked and the decisions are recorded. The exit condition
> was met by hand on 2026-10-21; the automated verifier has not yet produced
> a green run, and two Phase 0 benchmark runs disagree about which model
> reads documents (F-032). See
> [`docs/phase0-runbook.md`](docs/phase0-runbook.md) for the infra side,
> [`docs/ocr-decision.md`](docs/ocr-decision.md) for the model decision
> and the privacy architecture, and
> [`IMPLEMENTATION.md`](IMPLEMENTATION.md) for the full plan.

### Phase 0 decisions

| Decision | Outcome | Record |
|---|---|---|
| Which model reads documents? | `nvidia/Nemotron-3.5-Super-VL` — 99% field recovery, $0.30/1M in | [`docs/ocr-decision.md`](docs/ocr-decision.md) |
| How do personal details stay inside the sandbox? | Extract locally → redact locally → the model only ever reads redacted text | [`docs/ocr-decision.md`](docs/ocr-decision.md) |
| What does the deterministic redactor miss? | Names, alphanumeric account numbers, policy numbers, city and postcode — **0% detected** | [`docs/redaction-audit.md`](docs/redaction-audit.md) |
| Which model IDs do we use? | Pinned from the live catalog, never hand-typed | [`docs/model-catalog.md`](docs/model-catalog.md) |

The last row matters more than it looks: Nebius's web catalog, its API
catalog and its cookbook disagree about which models exist, and the same
model is published under several casings. Every model ID in this repo
comes from the live catalog.

---

## The three claims

| # | Claim | Where it is proven | Phase |
|---|---|---|---|
| 1 | Personal details do not leak out of the agent | `eval/leak_test.py` | 2 |
| 2 | A prompt-injected agent cannot exfiltrate your data, and the attempt is on the record | `sandbox/middleware/ledger-router/` | 2 |
| 3 | The agent's memory is right when the facts change, not just when they are recalled | `eval/memory_updates_eval.py` | 3 |

Plus a cost/latency table across three model tiers
(`eval/cost_latency_bench.py`).

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
| main agent reasoning | `nvidia/nemotron-3-super-120b-a12b` | 0.30 / 0.90 |
| hardest reasoning | `nvidia/Nemotron-3-Ultra-550b-a55b` | 1.00 / 3.00 |
| **document OCR** | **`nvidia/Nemotron-3.5-Super-VL`** | 0.30 / 0.90 |

The OCR model is the Super tier: it is multimodal, and it is the model the
decision record ([`docs/ocr-decision.md`](docs/ocr-decision.md)) names and
the Phase 2 privacy architecture was validated against. The choice was
measured, not preferred — 20 documents, five vision models, 99.0% field
recovery.

A second, independent run (30 rendered scans) preferred
`openbmb/MiniCPM-V-4_5` — 98.0% field recovery at $0.00050 per document
against Super-VL's 96.0% / $0.00124, with the local `tesseract` fallback
measured at 86.7% and zero egress. That comparison is **open** (F-032):
re-deciding it means re-validating the privacy architecture against a
different reader, so it is the first item carried into Phase 1. Two models
named in the original plan do not exist on Token Factory at all; see the
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
├── agent/                        the pipeline
│   ├── ingestion/                parse_documents.py, parse_email.py
│   ├── memory/                   hermes_memory.py, update_tracker.py
│   ├── deadlines/                flag_deadlines.py
│   └── actions/                  draft_actions.py
│
├── privacy_router/               Phase 2 — redaction and guardrails
│   ├── redact.py                 the redaction library
│   ├── injection_guard.py        exfiltration detection
│   └── audit_log.py              normalizes OpenShell's audit events
│
├── search/                       Tavily, routed through the router
├── jobs/                         Nebius Serverless Jobs — deadline monitoring
├── interface/                    Telegram/Slack bot + web view
├── eval/                         the three proofs + the synthetic corpus
└── docs/                         runbook, friction log, architecture
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

---

## Licence

Apache-2.0 — see [`LICENSE`](LICENSE).

<!-- The friction log, in full, is required in the final submission. -->
