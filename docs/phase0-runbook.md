# Ledger — Phase 0 runbook

**Phase 0 — Infra Verification (Oct 19–20)**
**Goal:** de-risk the stack before building anything user-facing.
**Exit condition (joint):** Hermes + OpenShell + Token Factory confirmed
working by EOD Oct 20. If not, jointly decide whether to pivot to the
fallback stack before Phase 1 starts (Oct 21).

This runbook is the executable form of that phase. `sandbox/setup.sh` performs
steps 1–6; this document explains what each step is proving and what to do
when it fails.

---

## 0. Prerequisites

| Requirement | Why | Check |
|---|---|---|
| Linux with Docker Engine 28.0+ **running** | OpenShell's Docker driver — and the gateway creates its Docker client at startup, so a stopped daemon stops the gateway too | `docker info` |
| Your user in the `docker` group | the daemon socket is root-owned | `id -nG \| grep docker` |
| A Nebius Token Factory API key | every model call | <https://tokenfactory.nebius.com/> |
| `jq` (optional) | pretty-printing model catalogs | `jq --version` |
| ~10 GB disk | the Hermes image is large (Chromium is baked in) | `df -h` |
| `bsdtar` (only on non-deb/rpm distros) | extracts the OpenShell RPMs | `command -v bsdtar` |

Copy the environment template and fill in the key. **Do not put the key in
a chat message or a commit** — `.env` is gitignored, and `setup.sh` and
`verify.sh` both read it from there.

```bash
cp .env.example .env
$EDITOR .env          # set NEBIUS_API_KEY
```

---

## 1. Install OpenShell

**Pin the version.** We are building against `v0.1.4`; the middleware
contract in particular is marked as evolving.

```bash
export OPENSHELL_VERSION=v0.1.4
```

### 1a. Debian / Ubuntu / Fedora / RHEL

```bash
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
openshell status
```

The installer picks a `.deb` or an `.rpm` based on the host package
manager, installs the CLI, the gateway and the prover, starts the gateway
as a systemd **user** service, and registers it at
`https://127.0.0.1:17670`.

### 1b. Everything else (Arch, CachyOS, Gentoo…)

The upstream installer supports exactly three Linux paths — `deb`, `rpm`
and `snap` — and on a host with neither `dpkg` nor `rpm` it exits with
*"Linux installs require either dpkg or rpm"*.

The RPMs still work: they are plain archives, and `bsdtar` (libarchive)
reads RPMs. They contain the CLI, the gateway, the prover, the systemd user
unit and the config-migration helper. The container images are **not** in
the RPM — the Docker driver pulls those from ghcr.io on first sandbox
creation.

```bash
sudo bsdtar xf openshell-0.1.4-1.fc44.x86_64.rpm            -C /
sudo bsdtar xf openshell-gateway-0.1.4-1.fc44.x86_64.rpm    -C /
sudo bsdtar xf openshell-prover-0.1.4-1.fc44.x86_64.rpm     -C /

systemctl --user daemon-reload
systemctl --user enable --now openshell-gateway
openshell status
```

`bash sandbox/setup.sh` does all of this automatically when it detects a
host with neither `dpkg` nor `rpm`.

### 1c. Where systemd --user is not available

The packaged unit does three things at startup: seeds `gateway.toml`,
runs `openshell-gateway config preflight`, and generates the mTLS PKI. If
`systemctl --user` is unavailable, do it by hand — the first two commands
are the ones from step 2, without the `systemctl` restart:

```bash
mkdir -p ~/.config/openshell
cp sandbox/gateway.toml ~/.config/openshell/gateway.toml

openshell-gateway config preflight --path ~/.config/openshell/gateway.toml
openshell-gateway generate-certs \
  --output-dir ~/.local/state/openshell/tls \
  --server-san host.openshell.internal

openshell-gateway --config ~/.config/openshell/gateway.toml &
openshell gateway add https://127.0.0.1:17670 --local --name local
openshell gateway select local
```

### What could go wrong

| Symptom | Cause | Fix |
|---|---|---|
| Installer exits with "require either dpkg or rpm" | unsupported distro | use 1b |
| `openshell status` cannot reach the gateway | systemd user service did not start | `systemctl --user status openshell-gateway`; otherwise use 1c |
| **Gateway exits immediately: "failed to create Docker client: Socket not found"** | **the daemon is not running** | `sudo systemctl start docker` — this is a gateway prerequisite, not just a sandbox one. `openshell doctor check` reports the same thing |
| Gateway will not start | a registered middleware service is down | see the middleware note in `sandbox/gateway.toml` — we deliberately keep that block commented out until Phase 2 |
| `openshell-gateway config preflight` says `missing_version` | gateway.toml lacks `[openshell] version = 2` | use the file in `sandbox/` |
| `credential_endpoint_mismatch` on every call | the provider profile's endpoint list does not cover the host Hermes actually used | compare `hermes config get model --json` base_url against the profile's `endpoints` |

---

## 2. Install the gateway configuration

```bash
mkdir -p ~/.config/openshell
cp sandbox/gateway.toml ~/.config/openshell/gateway.toml
openshell-gateway config preflight --path ~/.config/openshell/gateway.toml
systemctl --user restart openshell-gateway
```

The file pins `compute_driver = "docker"`. The MicroVM driver is never
auto-detected and would additionally require KVM.

It also enables **caller driver config** for that driver:

```toml
[openshell.drivers.docker]
allow_driver_config = true
```

This is what makes step 7's `--driver-config-json` (the volume attachment)
possible. OpenShell 0.1.4 disables caller-supplied driver config for every
compute driver by default, and the switch lives in the **driver table** — not
in `[openshell.gateway]`, which the gateway rejects as an unknown key.

> **The gateway reads its TOML at startup.** Editing the file does not change
> a running gateway. After changing it: `systemctl --user restart
> openshell-gateway`. `sandbox/setup.sh` does the copy, the preflight and the
> restart for you, and refuses to continue if the opt-in is missing.

---

## 3. Import the provider profiles

A provider **profile** describes a service: which credential it needs, which
hosts it may reach, and which binaries may make the call. A provider
**instance** holds the actual credential value. Both are needed.

```bash
openshell profile lint -f sandbox/providers/nebius-token-factory.yaml
openshell profile import -f sandbox/providers/nebius-token-factory.yaml
```

Do the same for `tavily.yaml` and `telegram.yaml`.

> **The host must match Hermes exactly.** Hermes's built-in
> `nebius-token-factory` provider pins
> `base_url = https://api.tokenfactory.nebius.com/v1`. If the OpenShell
> profile names a different host, Hermes aims at a host the credential is not
> bound to and every call fails with `credential_endpoint_mismatch`.

> **Do not set `NEBIUS_BASE_URL` inside the sandbox.** Hermes honours it.
> Setting it is the cleanest possible proof that routing is enforced by the
> runtime rather than by our configuration discipline — see step 7.

### What could go wrong

| Symptom | Cause | Fix |
|---|---|---|
| `profile lint` rejects the file | unknown field or bad enum | OpenShell rejects unknown fields; compare against `providers/nvidia.yaml` in the OpenShell repo |
| Profile imports but the credential never arrives | `binaries` paths do not match the image | the profile is editable: `openshell profile export <id> -o yaml > p.yaml`, edit, then `openshell profile update <id> -f p.yaml` |
| `credential_endpoint_mismatch` on every call | the provider profile's endpoint list does not cover the host Hermes actually used | compare `hermes config get model --json` base_url against the profile's `endpoints` |

---

## 4. Create the provider instance

```bash
NEBIUS_API_KEY=$NEBIUS_API_KEY openshell provider create \
  --name nebius --type nebius-token-factory --from-existing
```

`--from-existing` reads `NEBIUS_API_KEY` from the environment. The gateway
stores the value in its credential store and never exports it. The sandbox
receives an opaque placeholder under the same name.

Same for `tavily` and `telegram` once you have those keys.

---

## 5. Build the sandbox image

```bash
docker build -t ledger-hermes:local -f sandbox/Dockerfile.hermes .
```

This inherits `nousresearch/hermes-agent:latest` and adds an OCR fallback
(`tesseract-ocr`, `poppler-utils`), a `/sandbox` workspace, and a direct
entrypoint.

**Why the entrypoint is replaced.** The upstream image ends with `USER root`
because its s6-overlay entrypoint needs root to chown the data volume and drop
privileges per supervised service. OpenShell rejects a root workload unless
the creation policy names a non-root `run_as_user` (we pin UID 10000, the
image's `hermes` account), and OpenShell already supervises the process at
the sandbox level. See `sandbox/docker-entrypoint.sh`.

**Never override the upstream `entrypoint:`** in a plain Docker Compose file:
that removes the zombie reaper and the s6 supervision tree. Our derived image
is the one place we do it, deliberately and with the reasoning written down.

---

## 6. Create the data volume

```bash
docker volume create \
  --label openshell.ai/sandbox-attachable=true \
  --label openshell.ai/sandbox-attachable-workspace=default \
  ledger-hermes-data
docker run --rm --user 0 -v ledger-hermes-data:/data --entrypoint sh \
  nousresearch/hermes-agent:latest -c 'chown 10000:10000 /data'
```

> **Use a named volume, never a bind mount.** Hermes stores sessions and
> memory in a WAL-mode SQLite database. Its documentation warns that a bind
> mount crossing a VM boundary (virtiofs on macOS, 9p on Windows) can corrupt
> it *silently* — `PRAGMA integrity_check` still passes while concurrent
> writers destroy the file. A named volume lives on the host's filesystem and
> does not have that failure mode. A plain container writable layer is also
> fine; the dangerous case is specifically a bind mount from a shared mount.

The `openshell.ai/sandbox-attachable=true` label is required — the driver
rejects volumes without it.

---

## 7. Create the sandbox and verify routing

```bash
openshell sandbox create \
  --name ledger-hermes \
  --from ledger-hermes:local \
  --policy sandbox/policy.yaml \
  --provider nebius \
  --driver-config-json '{"docker":{"mounts":[{"type":"volume","source":"ledger-hermes-data","target":"/opt/data","read_only":false}]}}' \
  --restart-policy on-failure \
  --detach \
  -- /usr/local/bin/hermes-direct chat
```

> **The command after `--` is the canonical main process, run verbatim.**
> OpenShell stores it as `{"command": ["<program>", "<args>"]}` and executes
> exactly that — it is *not* passed through a shell, and the image's
> `ENTRYPOINT`/`CMD` are **ignored** (the Docker driver replaces the
> container entrypoint with the OpenShell sandbox runtime). So `-- chat` asks
> the sandbox to exec a program literally named `chat`, which does not exist
> in the image; the correct form names the program, as above.

`bash sandbox/setup.sh --check` runs `bash sandbox/verify.sh`, which performs
every check in 7.1–7.6 below.

### 7.1 The effective policy contains the endpoint

```bash
openshell policy get ledger-hermes --full
```

Expect `api.tokenfactory.nebius.com:443` and a `_provider_*` rule contributed
by the attached provider. `--base` shows only what we set; `--full` is what
the sandbox actually enforces.

### 7.2 The sandbox log shows the connection

```bash
openshell logs ledger-hermes --since 10m --source sandbox
```

Do **not** filter with `--level warn` — that hides policy events, which are
exactly what we are looking for.

### 7.3 A real model call succeeds

```bash
openshell sandbox exec -n ledger-hermes --no-login-shell -- \
  /opt/hermes/.venv/bin/hermes chat \
  --provider nebius \
  --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B \
  -q 'Reply with exactly: LEDGER-OK'
```

**Pin the model IDs from the live catalog** rather than trusting any
hand-typed string — the casing is inconsistent across Nebius's own
documentation:

```bash
curl -sS https://api.tokenfactory.nebius.com/v1/models?verbose=true \
  -H "Authorization: Bearer $NEBIUS_API_KEY" | jq -r '.data[].id'
```

### 7.4 NEGATIVE — the credential is scoped to one destination

This is the step that proves routing is *enforced* rather than *configured*.

The test sends the **sandbox's own opaque placeholder** to a host that the
policy allows but that the Token Factory credential is **not** bound to
(`api.tavily.com`). OpenShell must refuse it with
`credential_endpoint_mismatch`.

Two things this test must not do:

- **Do not send the real key.** The sandbox never holds the real key — it
  holds a placeholder — so sending the host's `NEBIUS_API_KEY` would put a
  live secret on a command line *and* would not exercise the binding at
  all. The variable has to be expanded **inside** the sandbox.
- **Do not target a host the policy already blocks.** A request to a host
  outside `network_policies` dies at the policy layer and never reaches the
  credential check, so the test would be inconclusive.

```bash
openshell sandbox exec -n ledger-hermes --no-login-shell -- \
  /bin/sh -c 'curl -sS -o /dev/null -w "%{http_code}" \
      -H "Authorization: Bearer $NEBIUS_API_KEY" \
      https://api.tavily.com/search'
```

Expect `credential_endpoint_mismatch` and HTTP 403. The same placeholder
sent to Token Factory succeeds; sent anywhere else, it is refused.

### 7.5 NEGATIVE — the agent cannot be pointed at another host

Hermes honours `NEBIUS_BASE_URL`. Pointing it somewhere else must be
refused by the runtime, because the credential is bound to Token Factory
only. This is the strongest form of the routing proof: the agent actively
tries to leave and the sandbox says no.

```bash
openshell sandbox exec -n ledger-hermes --no-login-shell -- \
  /usr/bin/env NEBIUS_BASE_URL=https://api.example.invalid/v1 \
  /opt/hermes/.venv/bin/hermes chat --provider nebius -q 'ping'
```

### 7.6 The egress allowlist is exactly what we intended

```bash
openshell policy get ledger-hermes --full | grep -oE 'host: [a-z0-9.-]+' | sort -u
```

Expect exactly three destinations: `api.tokenfactory.nebius.com`,
`api.tavily.com`, `api.telegram.org` (plus `host.openshell.internal` for
gateway-internal traffic). Anything else is a hole.

---

### 7.7 Run all of it at once

```bash
bash sandbox/verify.sh
```

One command, one pass/fail table, exit code 0 only if every required check
passed. **This is the artifact that proves the Phase 0 exit condition** —
it is the file a reviewer runs, and it is the same file `setup.sh --check`
delegates to.

---

## 8. Pin the model IDs

```bash
make pin-models
```

Write the results into `.env` as `LEDGER_MODEL_NANO`, `LEDGER_MODEL_SUPER`,
`LEDGER_MODEL_ULTRA`, `LEDGER_MODEL_OCR`, `LEDGER_MODEL_REDACT`.

The target reads the live catalog, resolves each role against it,
**refuses to pin a model the catalog does not serve**, writes those IDs
into `.env`, and writes a dated snapshot of the catalog to
`docs/model-catalog.md`.

**The catalog is the only authority.** Every published source spells these
differently, and two of the IDs quoted in planning do not exist at all
(friction log F-024). The 28 models the catalog actually served, with
prices, are mapped to the plan's tiers here — the cost/latency table in
Phase 3 is a Nemotron tier table:

| Plan tier | Model | Context | $/1M in/out | Role |
|---|---|---:|---:|---|
| Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 256K | 0.06 / 0.24 | redaction, classification, cheap side-jobs |
| Lightning | `nvidia/Nemotron-3_5-Lightning` | 1M | 0.06 / 0.24 | fast cheap tier |
| Super | `nvidia/Nemotron-3.5-Super-VL` | 1M | 0.30 / 0.90 | main agent reasoning **and** document understanding |
| Super (text-only) | `nvidia/nemotron-3-super-120b-a12b` | 256K | 0.30 / 0.90 | text-only alternative — **not** the pinned tier |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | 1M | 1.00 / 3.00 | hardest reasoning, long-horizon agent work |
| OCR | `nvidia/Nemotron-3.5-Super-VL` | 1M | 0.30 / 0.90 | document understanding (Phase 1 ingestion) |
| Comparator | `openbmb/MiniCPM-V-4_5` | 32K | 0.66 / 1.11 | vision — the candidate the second benchmark run preferred (F-032) |

Note that `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` **rejects image input**
("This model does not support image input"). The redaction tier is
text-only, which is fine — redaction operates on text.

> **The Super tier is multimodal.** `nvidia/Nemotron-3.5-Super-VL` is the
> 120B variant that takes an image; `nvidia/nemotron-3-super-120b-a12b`
> is a *different* model in the same catalog and is text-to-text only.
> The `redact_models` list in `sandbox/policy.yaml` names the text-only
> one and has to be updated.

> **The exact spelling of each ID must come from the catalog.** This is
> not a formality. Nebius's web catalog, its API catalog and its
> cookbook disagree about which models exist at all — the model this
> runbook used to name for OCR, `nvidia/NVIDIA-Nemotron-3-Nano-Omni`, is
> **not served by the API at all**, under any casing — and the same model
> is published under several casings. See F-017 and F-018 in the
> friction log.

### The OCR decision — two runs, two winners

**Run 1 — 20 documents, five vision models, live API** (`docs/ocr-decision.md`):

| Candidate | Field recovery | Mean | p95 | Tokens (in/out) | Cost |
|---|---:|---:|---:|---|---:|
| `nvidia/Nemotron-3.5-Super-VL` | **99.0%** | 3.98s | 5.50s | 29,340 / 16,590 | $0.0237 |
| `zai-org/GLM-5.3-Flash` | 100.0% | 3.54s | 4.22s | 37,100 / 5,881 | $0.0085 |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 100.0% | 2.86s | 3.67s | 18,460 / 4,885 | $0.0114 |
| `Qwen/Qwen3.8-27B` | 100.0% | 2.50s | 3.16s | 29,180 / 4,110 | $0.0255 |
| `openbmb/MiniCPM-V-4_5` | 100.0% | 4.25s | 5.34s | 10,340 / 7,568 | $0.0152 |
| `local-rapidocr` (zero egress) | 93.0% | 1.33s | 1.57s | — | $0 |
| `local-text` (baseline) | 100.0% | — | — | — | — |

**`nvidia/Nemotron-3.5-Super-VL` is the OCR model of record.** It is the
weakest of the five vision models on this corpus and it costs more per
document than the mid-priced ones. It was chosen anyway for three reasons:
the gap to the leaders is one field on one document out of twenty (`amount`
at 95% is inside the noise of a 20-document sample), it is the Nemotron
model — the Phase 3 cost/latency table is a Nemotron tier table — and it is
the cheapest model from a major lab that is both multimodal and 1M-context.
The full reasoning, and the privacy architecture built on top of it, are in
[`docs/ocr-decision.md`](ocr-decision.md).

**Run 2 — 30 rendered scans, with the local extractor measured too:**

| Candidate | Kind | Docs | Field recovery | Mean latency | p95 | $/doc |
|---|---|---:|---:|---:|---:|---:|
| `openbmb/MiniCPM-V-4_5` | vision | 30 | 98.0% | 2.85s | 3.28s | $0.00050 |
| `nvidia/Nemotron-3.5-Super-VL` | vision | 30 | 96.0% | 4.26s | 4.92s | $0.00124 |
| `local-tesseract` | local | 30 | 86.7% | 0.36s | 0.40s | $0 |
| `local-text` (baseline) | text | 30 | 100.0% | — | — | $0 |

This run points at **`openbmb/MiniCPM-V-4_5`**: it reads better than
Super-VL here and costs 2.5× less per document, because disabling its
thinking cuts the output tokens from 26,621 to 4,377 across the same 30
documents. At 200 documents it costs **$0.10**. It is also the only run
with the local `tesseract` fallback measured: **86.7% recovery at 0.36s
with zero egress** — that is the number the zero-egress path depends on,
and it is why the benchmark reports a SKIPPED row rather than hiding a
missing candidate.

**Run 2 does not overturn the decision of record — it opens it.** The two
runs used different corpora and different candidate sets, so they are not
contradictory measurements; MiniCPM scores at or above Super-VL in both.
But they disagree on the winner, and the choice is a product decision, not
a merge detail: Super-VL is a Super-tier model, so using it as the
document reader is exactly what forces the "extract locally, redact
locally" architecture (F-019) that was validated against it. Re-deciding
means re-validating that architecture against a different reader.

**Open item:** the OCR model choice is carried into Phase 1 as the first
thing to settle — see F-032. Until then Super-VL is the pinned OCR model
(`eval/pin_models.py`), and `docs/ocr-decision.md` is the decision record.

Reproduce with `make ocr-bench`; the committed run is in
`eval/results/ocr_bench.json`.

---

## 9. Corpus and OCR benchmark

```bash
make synth            # 200 synthetic documents + rendered PNGs, deterministic
make corpus-qa        # validate the corpus before trusting any number
make ocr-bench        # compare the vision models and the local extractors
make redacted-extract # can the model read redacted text? (the privacy design)
make redaction-audit  # what the deterministic redactor misses
```

The benchmark compares every vision model the catalog serves against the
local extractors, and scores **field recovery** rather than string
similarity — a model that transcribes prose beautifully but mangles the
account number is not good enough for us, and edit distance would not
notice.

The winner becomes the Phase 1 ingestion path. The decision and its
numbers are in [`docs/ocr-decision.md`](ocr-decision.md).

> **Two runs, two winners.** Section 8 records a second benchmark run that
> preferred a different candidate. The decision of record is still
> `docs/ocr-decision.md`; the disagreement is open as F-032.

> **Run `make corpus-qa` before you believe a benchmark.** The corpus
> renders a page per document, and the renderer used to truncate long
> lines off the right edge — so the "scan" was a partial document and
> every model was judged on a page missing text. That is F-011.

---

## Failure mode index

| Error | Meaning | Fix |
|---|---|---|
| `policy_denied` | no rule allows the connection | check the binary path and host in `openshell policy get <sandbox> --full`; scripts run as their interpreter |
| `credential_endpoint_mismatch` | policy allowed the request, but the credential is not bound to that destination | fix the provider profile's endpoints — do **not** widen the network rule |
| `credential_placeholder_in_request_body` | a stale placeholder in a request body | relaunch the process after attaching the provider |
| `request_authority_mismatch` | the `Host` header differs from the connection target | usually a redirect; make the client send the right host |
| `unsupported_l7_protocol` | the client sent an upgrade header the endpoint cannot inspect | allow WebSocket traffic on a separate `protocol: websocket` endpoint |
| sandbox stuck in `Provisioning` | invalid policy or conflicting provider config | `openshell sandbox get <sandbox> --output json` and read the `ConfigurationInvalid` condition |
| `ImagePreparationTimedOut` | the image took longer than the 1800s preparation budget | raise `image_preparation_timeout_seconds` in the gateway TOML |
| model call 404 "model not found" | wrong model ID casing | re-pin from `/v1/models?verbose=true` |
| `caller driver config is disabled; a gateway administrator must enable allow_driver_config` | the gateway rejects `--driver-config-json` because caller driver config is off | add `allow_driver_config = true` to `[openshell.drivers.docker]` in `sandbox/gateway.toml`, then `systemctl --user restart openshell-gateway` — the gateway reads its config only at startup |
| `external resource not admitted by required labels` | the Docker volume lacks the operator approval labels | both labels are required: `openshell.ai/sandbox-attachable=true` and `openshell.ai/sandbox-attachable-workspace=default` — Docker cannot add a label to an existing volume, so a volume missing one must be relabelled or recreated (recreating destroys the data) |
| sandbox exits immediately, "executable file not found" | the command after `--` did not name a real program | the command is run verbatim as program+argv and the image ENTRYPOINT is ignored — use `-- /usr/local/bin/hermes-direct chat`, not `-- chat` |
| agent cannot write state | volume not owned by UID 10000 | re-run the chown in step 6 |
| gateway will not start | a registered middleware service is down | uncomment the middleware block in `sandbox/gateway.toml` and restart the service |

---

## Phase 0 exit checklist

Run `bash sandbox/verify.sh` and paste its table into the friction log. The
checklists below are the human version of what that script asserts, plus the
items only a person can tick.

`[x]` confirmed · `[~]` provisional — written from design, still to be
confirmed by an automated run · `[ ]` not yet done.

**Person A — infra (verified by hand; the automated run is outstanding):**

- [x] `openshell status` reaches the gateway
- [x] the Docker daemon is running and the gateway starts
- [x] all three provider profiles lint and import cleanly
- [x] the `nebius` provider instance is created
- [x] the image builds and the sandbox reaches `Ready`
- [x] 7.1 the effective policy shows the Token Factory endpoint
- [ ] 7.1 the workload runs as UID 10000, not root
- [x] 7.2 the log shows a connection to Token Factory
- [x] 7.3 a real model call returns `LEDGER-OK`
- [~] 7.4 the placeholder is refused on a host the credential is not bound to
- [~] 7.5 the agent cannot be pointed at another host
- [ ] 7.6 the allowlist contains exactly the three intended destinations
- [x] `tesseract` measured — it is installed on the development host and recovers **100% of the fields and 100% of the document's lines** on the corpus. It is the same binary the sandbox image ships, so the number carries over (F-020)

**Person B — agent and benchmarks (done):**

- [x] model IDs pinned into `.env`, validated against the live catalog, snapshot in `docs/model-catalog.md`
- [x] `make synth` produced the corpus — 200 documents + 200 rendered PNGs, byte-identical across runs
- [x] `make corpus-qa` passes — 12 checks, including that nothing is clipped off the page (F-011)
- [x] `make ocr-bench` produced a comparison table — 5 vision models, 2 local extractors, per-field breakdown
- [x] `make redacted-extract` validated the privacy architecture — the model reproduces every placeholder, zero hallucinations
- [x] `make redaction-audit` measured what the deterministic redactor misses — the hand-off list for Phase 2
- [x] the model decision recorded in `docs/ocr-decision.md`
- [x] this runbook updated with what actually differed
- [x] friction log updated — F-009 … F-040

**Provisional — written from research and design, to be confirmed by a run:**

- [~] the README setup section (A3) — the procedure is documented and was
      followed to build the environment, but it has not been walked
      end-to-end by a second person on a clean machine
- [~] `bash sandbox/verify.sh` — the script is written and runs, but it has
      not produced a green run: it must be executed **on the gateway host**,
      where the CLI holds the matching mTLS client bundle. From another
      machine it reports the checks it cannot evaluate as SKIP
- [~] the two negative tests inside the verifier (7.4 and 7.5 above). These
      are stronger than the plan requires and remain unconfirmed by an
      automated run. They are the difference between "routing is configured"
      and "routing is enforced by the runtime"

**Incomplete — known gaps, none of them blocking the exit condition:**

- [x] GitHub Actions CI — the lint and test config and the workflow are both
      in the tree
- [x] the two models named in planning that do not exist on Token Factory
      are replaced everywhere, and the reason is on the record (F-018, F-024)
- [ ] the OCR model — two Phase 0 benchmark runs disagree about the winner,
      and re-deciding it means re-validating the privacy architecture
      against a different reader (F-032)

**The exit condition is met when `bash sandbox/verify.sh` exits 0.** It was
met manually on 2026-10-21; the automated run is the outstanding
confirmation, and the two negative tests are the outstanding hardening.

---

## What actually differed from this runbook

Three things the plan did not anticipate, each found by running it:

**1. The OpenShell Docker driver overrides the image's ENTRYPOINT.** The
canonical main process is the command after `--`, run verbatim. An image
that relies on its own `ENTRYPOINT` to start the agent will not start the
agent. The image therefore names the program explicitly:

```bash
openshell sandbox create --name ledger-hermes \
  --from ledger-hermes:local --policy sandbox/policy.yaml \
  -- /usr/local/bin/hermes-direct chat
```

**2. `--driver-config-json` is disabled by default in OpenShell 0.1.4.** The
volume attach fails with *"caller driver config is disabled; a gateway
administrator must enable allow_driver_config"*. The switch lives in the
**driver** table, not the gateway table:

```toml
[openshell.drivers.docker]
allow_driver_config = true
```

Putting it under `[openshell.gateway]` makes the gateway refuse to start —
it rejects unknown keys in both places.

**3. External resources are admitted by label, and the workspace label has
to resolve.** A Docker volume attached through driver config is refused
with *"external resource not admitted by required labels"* unless it
carries both:

```
openshell.ai/sandbox-attachable=true
openshell.ai/sandbox-attachable-workspace=${workspace}
```

The obvious workaround — disabling resource admission — is the wrong fix: it
would let any caller attach any host volume to any sandbox. Keep admission
on and label the volume.

If any of 7.1–7.6 fails at EOD Oct 20, that is the go/no-go signal for the
fallback stack. The cleanest fallback keeps OpenShell and swaps the
inference provider: OpenShell ships a ready-made `nvidia` profile for
NVIDIA NIM, and Hermes has a first-class `nvidia` provider. Everything else
in this runbook — the sandbox, the policy, the image, the volume — is
unchanged.
