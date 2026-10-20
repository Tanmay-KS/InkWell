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
| Linux with Docker Engine 28.0+ | OpenShell's Docker compute driver | `docker version` |
| A Nebius Token Factory API key | every model call | <https://tokenfactory.nebius.com/> |
| `jq` (optional) | pretty-printing model catalogs | `jq --version` |
| ~10 GB disk | the Hermes image is large (Chromium is baked in) | `df -h` |

Copy the environment template and fill in the key:

```bash
cp .env.example .env
$EDITOR .env          # set NEBIUS_API_KEY
```

---

## 1. Install OpenShell

```bash
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
openshell status
```

The installer puts the CLI on `PATH`, installs a local gateway, and registers
it at `https://127.0.0.1:17670`.

**Pin the version.** We are building against `v0.1.4`; the middleware
contract in particular is marked as evolving.

```bash
export OPENSHELL_VERSION=v0.1.4
```

On a distribution that is not Debian/Ubuntu or Fedora/RHEL there is no
package — take the release tarball instead:

```bash
curl -LsSf -o openshell.tar.gz \
  "https://github.com/NVIDIA/OpenShell/releases/download/v0.1.4/openshell-x86_64-unknown-linux-musl.tar.gz"
```

### What could go wrong

| Symptom | Cause | Fix |
|---|---|---|
| Installer picks the wrong package | unsupported distro | use the tarball |
| `openshell status` cannot reach the gateway | systemd user service did not start | `systemctl --user status openshell-gateway` |
| Gateway will not start | a registered middleware service is down | see the middleware note in `sandbox/gateway.toml` — we deliberately keep that block commented out until Phase 2 |
| `openshell-gateway config preflight` says `missing_version` | gateway.toml lacks `[openshell] version = 2` | use the file in `sandbox/` |

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
  -- chat
```

`bash sandbox/setup.sh --check` runs all four verification steps below.

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

### 7.4 NEGATIVE — the credential is not usable anywhere else

This is the step that proves routing is *enforced* rather than *configured*.

```bash
openshell sandbox exec -n ledger-hermes --no-login-shell -- \
  /usr/bin/curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $NEBIUS_API_KEY" https://example.com/
```

Expect `403` and a `credential_endpoint_mismatch` in the log. The same
placeholder sent to Token Factory succeeds; sent anywhere else, it is refused.

A second version of the same proof, this time from inside the agent's
configuration:

```bash
openshell sandbox exec -n ledger-hermes --no-login-shell -- \
  env NEBIUS_BASE_URL=https://api.example.invalid/v1 \
  /opt/hermes/.venv/bin/hermes chat --provider nebius -q 'ping'
```

Hermes honours `NEBIUS_BASE_URL` and aims at the wrong host; OpenShell
refuses the request because the credential is bound to Token Factory only.

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

The three tiers map to the plan's cost/latency table in Phase 3:

| Plan tier | Model | Role |
|---|---|---|
| Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | redaction, classification, cheap side-jobs |
| Super | `nvidia/Nemotron-3.5-Super-VL` | main agent reasoning **and** document understanding |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | hardest reasoning, long-horizon agent work |
| OCR | `nvidia/Nemotron-3.5-Super-VL` | document understanding (Phase 1 ingestion) |

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
| agent cannot write state | volume not owned by UID 10000 | re-run the chown in step 6 |
| gateway will not start | a registered middleware service is down | uncomment the middleware block in `sandbox/gateway.toml` and restart the service |

---

## Phase 0 exit checklist

**Person A — infra (not yet run):**

- [ ] `openshell status` reaches the gateway
- [ ] all three provider profiles lint and import cleanly
- [ ] the `nebius` provider instance is created
- [ ] the image builds and the sandbox reaches `Ready`
- [ ] 7.1 the effective policy shows the Token Factory endpoint
- [ ] 7.2 the log shows a connection to Token Factory
- [ ] 7.3 a real model call returns `LEDGER-OK`
- [ ] 7.4 the credential is refused on a foreign host
- [ ] `tesseract` measured **inside the sandbox image**, so the local path's accuracy is known in the environment it will actually run in (F-020)

**Person B — agent and benchmarks (done):**

- [x] model IDs pinned into `.env`, validated against the live catalog, snapshot in `docs/model-catalog.md`
- [x] `make synth` produced the corpus — 200 documents + 200 rendered PNGs, byte-identical across runs
- [x] `make corpus-qa` passes — 12 checks, including that nothing is clipped off the page (F-011)
- [x] `make ocr-bench` produced a comparison table — 5 vision models, 2 local extractors, per-field breakdown
- [x] `make redacted-extract` validated the privacy architecture — the model reproduces every placeholder, zero hallucinations
- [x] `make redaction-audit` measured what the deterministic redactor misses — the hand-off list for Phase 2
- [x] the model decision recorded in `docs/ocr-decision.md`
- [x] this runbook updated with what actually differed
- [x] friction log updated — F-009 … F-020

If any of 7.1–7.4 fails at EOD Oct 20, that is the go/no-go signal for the
fallback stack. The cleanest fallback keeps OpenShell and swaps the
inference provider: OpenShell ships a ready-made `nvidia` profile for
NVIDIA NIM, and Hermes has a first-class `nvidia` provider. Everything else
in this runbook — the sandbox, the policy, the image, the volume — is
unchanged.
