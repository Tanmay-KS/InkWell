# Ledger

**A privacy-routed personal agent for deadlines, renewals, and entitlements.**

Ledger reads the documents that come at you — insurance policies, gym
memberships, subscriptions, licence renewals, medical renewals — notices
what is about to lapse, tells you, and drafts the action. It runs inside a
sandbox with a declarative egress policy, and a privacy router strips your
personal details from anything that leaves, so the expensive models and
third-party search engines never see them.

> **Status: Phase 0 — infra verification.**
> The stack is being de-risked before anything user-facing is built. See
> [`docs/phase0-runbook.md`](docs/phase0-runbook.md) for where things stand
> and [`IMPLEMENTATION.md`](IMPLEMENTATION.md) for the full plan.

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

# 3. check that model calls really route to Token Factory
openshell policy get ledger-hermes --full
openshell logs ledger-hermes --tail
openshell term                   # live view: gateways, providers, sandboxes, blocked traffic
```

`bash sandbox/setup.sh --dry-run` exercises everything except the steps
that need a real key.

### Requirements

- Linux with Docker Engine 28.0+ (OpenShell's Docker compute driver)
- A Nebius Token Factory API key — <https://tokenfactory.nebius.com/>
- ~10 GB of disk (the Hermes image bakes in Chromium)

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
│   ├── docker-entrypoint.sh      why we replace the upstream entrypoint
│   └── setup.sh                  idempotent setup; this is the README section
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

To be added in Phase 4.

<!-- The friction log, in full, is required in the final submission. -->
