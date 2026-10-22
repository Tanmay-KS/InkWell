# Friction log

Both people append to this file **all week**. It is required in the final
feedback section, and it is the only honest record of what actually happened.

Format: one entry per friction point. Keep it short — what broke, what it
cost, what we did. Date it. Name who hit it.

---

## Pre-Phase 0 — known risks carried in from research (Oct 19)

These are not yet observed failures. They are the things most likely to cost
us time, written down so that when one bites we recognise it instead of
re-diagnosing it.

### F-001 · Binary matching: the venv interpreter's real path
**Owner:** Person A · **Phase:** 0
OpenShell matches the **real** path of the executable that opens the
connection, not a symlink. Hermes runs from a uv-managed virtualenv, so
`/opt/hermes/.venv/bin/python` resolves somewhere else — likely
`/usr/local/bin/python3.x`. The provider profile lists several candidate
paths, but if the actual resolved path is not among them the credential is
never injected and traffic is denied. A script also runs as its interpreter,
so a `pip`-style rule must list Python, never `pip`.
**Cost if unresolved:** the model call fails with a confusing denial.
**Detection:** `readlink -f` inside the sandbox, then read the path
OpenShell names in `openshell logs` when it denies a connection.
**Fix:** edit the profile and re-import (`openshell profile export` →
edit → `openshell profile update`).

### F-002 · Model ID casing is inconsistent across Nebius's own docs
**Owner:** Person B · **Phase:** 0
The cookbook writes `nvidia/nemotron-3-nano-omni`; the cookbook README
writes `nvidia/Nemotron-3-Nano-Omni`; Hermes's own provider defaults to
`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`. Hand-typing a model ID is a
coin flip. A wrong ID returns a 404 that reads like a credential problem.
**Fix:** always pin from `GET /v1/models?verbose=true` (`make pin-models`).

### F-003 · The provider must be attached before the process starts
**Owner:** Person A · **Phase:** 0
A process that is already running does not receive a newly attached
provider's environment variable. Attaching a provider to a live sandbox and
then expecting the running agent to pick up the credential wastes a
debugging cycle every time.
**Fix:** attach, then relaunch the process. `openshell sandbox provider
attach <sandbox> nebius --wait` then `openshell sandbox exec …`.

### F-004 · `credential_endpoint_mismatch` is not a network problem
**Owner:** Person A · **Phase:** 0
This error means policy *admitted* the request but the provider profile did
not authorise the credential at that destination. The tempting fix — widen
the network rule — is exactly wrong, because the network rule was never the
problem.
**Fix:** fix the provider profile's endpoint list. Never widen the rule.

### F-005 · The gateway will not start if a registered middleware service is down
**Owner:** Person A · **Phase:** 2
The gateway calls every registered middleware service at startup to verify
capabilities and protocol compatibility, and refuses to start if one is
unavailable. A crashed router means a dead gateway.
**Mitigation:** the middleware block in `sandbox/gateway.toml` is
**commented out** until Phase 2, so a missing router cannot take the gateway
down during Phase 0/1. In Phase 2 the service gets a container restart
policy and is registered last.

### F-006 · Two credential proxies will fight
**Owner:** Person A · **Phase:** 1
Hermes ships its own egress proxy (`hermes egress`, the iron-proxy) and a
Docker network-isolation mode. OpenShell already provides credential
injection and egress policy. Running both means two layers rewriting
headers and two places to look when a call is denied.
**Fix:** do not enable Hermes's iron-proxy inside the sandbox.

### F-007 · Rate limits will bite during the leak test
**Owner:** Person B · **Phase:** 2
Token Factory's limits are dynamic: rolling 15-minute buckets, a baseline of
60 RPM / 400K TPM, auto-scaling up by ×1.2 per window while utilisation
stays above 80%, and a hard ceiling of 20× before Enterprise. The leak test
runs ~200 documents through the pipeline in two conditions with several
model calls each — that will exceed a baseline limit.
**Fix:** pace the run, watch the `x-ratelimit-remaining-*` headers, and use
the Batch API for the bulk pass if the API reference offers a higher limit.

### F-008 · Hermes's WAL database must not live on a bind mount
**Owner:** Person A · **Phase:** 0
Hermes keeps sessions and memory in a WAL-mode SQLite database. On a bind
mount crossing a VM boundary (virtiofs on macOS, 9p on Windows) concurrent
writers can corrupt it *silently* — `PRAGMA integrity_check` still passes.
This would surface much later as phantom memory corruption in Phase 3.
**Fix:** a named Docker volume, created with the `sandbox-attachable` label
and chowned to UID 10000 before first boot.

---

## Observed frictions

_(recorded during Phase 0, 2026-10-20 → 2026-10-21 — the first time anything in this repo was actually run)_



### F-009 · The benchmark could never have tested a vision model
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
`make synth` did not pass `--render`, so no PNGs existed. The benchmark fell back to the `.txt` files, base64-encoded them, and posted them as `data:image/png;base64,…`. Every vision candidate would have failed with an opaque HTTP 400, and the table would have said "0% accuracy" for all of them — which reads like "these models are terrible" rather than "the harness was broken".
**Cost if unresolved:** the entire OCR decision would have been made on a table that measured nothing, and it would have looked like real data.
**Detection:** the fallback printed a reassuring message — "no PNGs found — evaluating against the text files" — and the candidate list looked populated.
**Fix:** `make synth` renders; the benchmark refuses to send anything that is not an image (magic-byte check, not the file extension) and marks the candidate SKIPPED with the reason.

### F-010 · The amount field was counted as recovered no matter what
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
The scorer computed the truth's digits and then checked whether those digits appeared in the *truth value* — always true. The extracted text was never consulted. Every candidate scored a free 20% on every document.
**Cost if unresolved:** every accuracy number in the project would have been inflated by a fifth, and the margin between candidates would have been distorted.
**Detection:** the baseline candidate — which reads the exact source text — also scored below 100%. The baseline cannot fail, so if it does, the scorer is broken. That is now an explicit test (`tests/test_scoring.py`).
**Fix:** compare against the extracted text. The baseline is now 100% on every field, and that is asserted on every generated document.

### F-011 · The rendered "scan" was a truncated document
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
The renderer drew each source line as one unbroken run of text on a 1000px page and drew nothing else. Lines longer than the page ran off the right edge, and lines past the bottom were dropped. The amount on a long line — `A renewal fee of $266.64 per month applies.` — was simply not in the image.
**Cost if unresolved:** every OCR candidate was being judged on a page missing text. Super-VL scored 92% when the document was actually complete; it scores 99% once the page contains everything.
**Detection:** the amount field failed on exactly the two documents with the longest lines, for every candidate equally. A model-independent failure is a harness failure.
**Fix:** the text is wrapped to the page width and the page grows to fit. `make corpus-qa` now asserts that nothing is clipped, and a test asserts the wrap is lossless.

### F-012 · "The same seed" did not mean "the same corpus"
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
The generator defaults its reference date to `date.today()` and derives every renewal date from it, so the same seed on a different day produced a different corpus. The README and the module docstring both claimed the corpus was byte-identical.
**Cost if unresolved:** the Phase 2 leak-test baseline would not have been comparable across runs, and every "we re-ran it and got a different number" would have been unanswerable.
**Detection:** reading the generator's default rather than trusting the documentation.
**Fix:** the reference date is pinned (`CORPUS_TODAY` in the Makefile) and is part of the reproducibility contract. A test asserts that changing the date changes the corpus and that pinning it restores byte-identity.

### F-013 · A candidate that could not run disappeared from the table
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
The benchmark skipped the local OCR candidate when the tesseract binary was not installed, and printed a note to stderr. The table simply had no row for it. A missing row reads as "untested", not "unavailable", and the decision between the local and the vision path turns on exactly that row.
**Cost if unresolved:** Option C — extract locally, redact locally, then call the model — would have been decided without the number that justifies it.
**Detection:** the row was absent from the table and nothing marked it as a failure.
**Fix:** a candidate that cannot run is now a row, marked SKIPPED with the reason. The same shape as F-004: the tempting fix is to make the missing thing go away quietly, and that is exactly what hides the cause.

### F-014 · Normalisation made a name indistinguishable from an email address
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
The redacted-text bench looked up which placeholder stood for a ground-truth value by comparing normalised strings. `devin.sullivan@example.com` normalises to `devinsullivaneexamplecom`, and the holder's name `Devin Sullivan` normalises to `devinsullivan` — which is a prefix of the other. The bench therefore concluded that the *name* had been redacted as the *email*, and reported the model as hallucinating names it had never been shown.
**Cost if unresolved:** the Option C validation reported 30% accuracy and 14 hallucinations for a model that was in fact perfect, and the recommendation would have been made on that.
**Detection:** the expected value in the failure message was an `[EMAIL_1]` placeholder for a field that is a person's name.
**Fix:** the lookup matches raw text, not normalised text. The real finding underneath it is unchanged and is more serious: the name is *not* redacted at all — see F-015.

### F-015 · The deterministic redactor misses almost everything that identifies a person
**Owner:** Person B (found) → Person A (to fix) · **Phase:** 0/2 · **Status:** open
Measured over the 200-document corpus (`make redaction-audit`):

| Value | Caught |
|---|---|
| email | 200/200 (100%) |
| phone | 200/200 (100%) |
| street address | 200/200 (100%) |
| account number | 153/200 (76.5%) |
| **holder name** | **0/200 (0%)** |
| **policy number** | **0/25 (0%)** |
| **city** | **0/200 (0%)** |
| **postcode** | **0/200 (0%)** |

**Not one of the 200 documents comes out of the deterministic pass with no surviving personal value.** The text is reduced by 5.2%. `Category.NAME` exists in `privacy_router/redact.py` but has no pattern in `_PATTERNS`, so names are only ever caught by the model-assisted escalation — and the escalation is itself an outbound call carrying the source text. The alphanumeric account numbers (`POL-4825232-I`, `SUB-281674`) match neither `_ACCOUNT` nor `_ACCOUNT_GROUPED`.
**Cost if unresolved:** the Phase 2 leak-rate number will be bad, and worse, the project will not have known it was bad until the night before the submission.
**Detection:** the audit script, which was written for exactly this.
**Fix:** Person A's Phase 2. The priority order is set by this table: names first (0%), then alphanumeric identifiers (0%), then city/postcode (0%).

### F-016 · A parameter shadowed a function of the same name
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
`generate_corpus(..., render: bool = False)` shadowed the module-level `render(spec)` function that turns a record into text, so the generator raised `TypeError: 'bool' object is not callable` the moment it was called as a library rather than from the command line.
**Cost if unresolved:** the function could not be tested, and the tests are the only thing that would have caught F-011 and F-012.
**Detection:** the test suite, on the first run.
**Fix:** the parameter is `render_images`. The generator's entry point is now a function the tests can call.

### F-017 · Nebius's catalog, Nebius's documentation and Nebius's cookbook all disagree
**Owner:** Person B · **Phase:** 0 · **Status:** worked around
Three sources disagreed about which models exist. The API catalog (28 models) has `nvidia/Nemotron-3.5-Super-VL`; the documentation's own example uses `Qwen/Qwen2-VL-72B-Instruct`, which the catalog does not serve; the cookbook's quickstart points at `nvidia/nemotron-3-nano-omni` and at a regional endpoint (`api.tokenfactory.us-central1.nebius.com`) rather than the global one the OpenShell profile binds. The same model is published under several casings.
**Cost if unresolved:** a hand-typed model ID is a coin flip, and a 404 that reads like a credential problem will burn a debugging cycle every time.
**Detection:** the catalog was fetched and compared with the runbook's model table.
**Fix:** `make pin-models` reads the live catalog, resolves each role against it, refuses to pin a model the catalog does not serve, and writes a dated snapshot to `docs/model-catalog.md`. No model ID in this repo is hand-written. F-002 was right.

### F-018 · The model the runbook names for OCR does not exist
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
`docs/phase0-runbook.md:275` pinned `nvidia/NVIDIA-Nemotron-3-Nano-Omni` as the OCR tier. The catalog has no model by that name at all — not under any casing. The runbook also listed `Qwen/Qwen2.5-VL-72B-Instruct` as a comparison, and the catalog does not serve that either.
**Cost if unresolved:** Phase 1 would have been built against a model that 404s on the first call, inside a sandbox where a 404 is hard to distinguish from a credential problem.
**Detection:** the same catalog fetch.
**Fix:** the runbook's model table is updated to the decided ID, and the benchmark's candidate list is derived from the catalog rather than hardcoded.

### F-019 · A Super-tier model cannot be both the document reader and the redact target
**Owner:** Person B (found) → Person A (to implement) · **Phase:** 0/2 · **Status:** open
The headline claim is that personal details never reach a Super/Ultra tier. The document-reading model is Super-VL, which *is* a Super-tier model. The Phase 2 policy redacts traffic to Super and Ultra by model ID — so the OCR call and the redact target are the same knob.
**Cost if unresolved:** either the OCR call breaks (the router mangles a base64 PNG it cannot meaningfully redact) or the claim is false. Discovering this in Phase 2, with a submission deadline two days out, would have been expensive.
**Detection:** reading `sandbox/policy.yaml` against the model decision.
**Fix:** Option C — extract locally, redact locally, then send only redacted text to the model. Validated with `make redacted-extract`: the model reproduced every placeholder exactly (13/13, zero hallucinations) and extracted every clear field perfectly. The Super-tier call then carries no personal detail at all. See `docs/ocr-decision.md`.

### F-020 · No passwordless sudo, so the local OCR baseline could not be installed
**Owner:** Person B · **Phase:** 0 · **Status:** fixed
`tesseract` is not on the development host and `pacman -S` prompts for a password. The benchmark's local candidate was therefore SKIPPED — which is the F-013 scenario, one layer down.
**Cost if unresolved:** the local path's accuracy would be unknown, and Option C's fallback (a low-confidence page sent to Super-VL as an image) could not be sized.
**Detection:** `which tesseract` → nothing.
**Fix:** `rapidocr-onnxruntime` was installed as a pip-only, no-sudo, zero-egress alternative, and tesseract was installed later. **Having both turned out to be worth more than having either**: the pair is what exposed F-037, because a model-independent local candidate scoring *worse* than a pip-only one is a fixture problem, not a model problem. The sandbox image already ships `tesseract-ocr` and `poppler-utils`, so the production path is covered.

### F-021 · The gateway needs Docker at startup, not just for sandboxes
**Owner:** Person A · **Phase:** 0
The OpenShell gateway creates its compute-driver client during startup and
**exits** if it cannot: `failed to create Docker client: Socket not found:
/var/run/docker.sock`. A stopped Docker daemon therefore takes down the
gateway, not just sandbox creation — and the error names the socket, not
the missing daemon, which reads like a path problem.
**Cost if unresolved:** confusing failure, and a gateway that appears to
install fine and then silently dies.
**Detection:** `sudo systemctl start docker` then `openshell status`.
**Fix:** `sandbox/setup.sh` now runs a Docker preflight before anything
else, and `sandbox/verify.sh` checks it as a required precondition.

### F-022 · The upstream installer does not support Arch-family distributions
**Owner:** Person A · **Phase:** 0
`install.sh` supports exactly three Linux paths — `deb`, `rpm` and `snap` —
and on a host with neither `dpkg` nor `rpm` it exits with *"Linux installs
require either dpkg or rpm"*. There is no `pacman` path and no generic
tarball install documented.
**Cost if unresolved:** a dead end that looks like "OpenShell does not run
on this distro".
**Detection:** read `linux_package_method()` in `install.sh`.
**Fix:** the RPMs are plain archives and `bsdtar` (libarchive) reads them.
`sandbox/setup.sh` now detects the no-dpkg/no-rpm case and extracts the
RPMs itself. The packages carry the CLI, gateway, prover, systemd user
unit and config-migration helper; the container images come from ghcr.io
(`ghcr.io/nvidia/openshell/{sandbox,supervisor}:0.1.4` — note the tag has
no `v`).

### F-023 · A negative routing test can pass for the wrong reason
**Owner:** Person A · **Phase:** 0
The first version of the credential-scoping test sent the host's **real**
`NEBIUS_API_KEY` to `example.com`. Three separate problems: it put a live
secret on a command line; the sandbox holds an opaque *placeholder*, not
the real key, so the test never exercised the binding it was meant to
test; and `example.com` is not in `network_policies`, so the request died
at the policy layer and never reached the credential check.
**Cost if unresolved:** a Phase 0 exit condition that looks satisfied and
proves nothing — the worst possible failure mode for a privacy project.
**Detection:** the test asserted on a 403, and a policy denial also
produces a 403.
**Fix:** the test now expands `$NEBIUS_API_KEY` *inside* the sandbox and
sends the placeholder to `api.tavily.com` — allowed by policy, not bound to
the credential — and distinguishes `credential_endpoint_mismatch` from
`policy_denied`. See `sandbox/verify.sh` check 4.

### F-024 · Two of the models we planned do not exist on Token Factory
**Owner:** Person B · **Phase:** 0
The OCR model chosen in the planning conversation was
`nvidia/NVIDIA-Nemotron-3-Nano-Omni`, and Nebius's own document-
intelligence page names `Qwen2.5-VL` for OCR. **Neither is in the live
catalog.** The catalog has 28 models and only two of them accept images:
`nvidia/Nemotron-3.5-Super-VL` and `openbmb/MiniCPM-V-4_5`.
Every source we had quoted a model ID that is either absent or spelled
differently — the Nemotron cookbook writes `nvidia/nemotron-3-nano-omni`
while Hermes's own provider defaults to `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`.
**Cost if unresolved:** the Phase 1 ingestion pipeline is written against a
model that does not exist, and the failure surfaces as a 404 on the first
real document.
**Detection:** `GET /v1/models?verbose=true` is the only authority. Never
hand-type a model ID.
**Fix:** `make pin-models` reads the catalog; `eval/ocr_bench.py` compares
the two vision models that actually exist; the OCR model is pinned in
`.env` as `LEDGER_MODEL_OCR`.

### F-025 · A reasoning model will spend the whole completion budget thinking
**Owner:** Person B · **Phase:** 0/1 · **Status:** fixed
Two models, one mechanism, and it is the kind of failure that hides behind a number: a model that reasons before it answers can spend the entire completion budget thinking and return an empty string.

**`openbmb/MiniCPM-V-4_5` — the OCR path.** On a transcription task it can burn the entire completion budget inside a `<think>` block and return an empty string — 16,363 characters of trace and no answer, with the block left **unclosed**. The first version of the benchmark scored that as "zero fields recovered" and hid a real failure behind a number.
**Cost if unresolved:** an unusable OCR path, and a benchmark that reports a failure as a score.
**Detection:** the benchmark now treats an unclosed reasoning block as an error, not a zero.
**Fix:** `chat_template_kwargs: {"enable_thinking": false}` in the request body. A literal `/no_think` appended to the prompt does **not** work on this endpoint — it makes the model think more, not less, and every document failed. With the real control the same model went from 25/25 errors to 0/25 and dropped from 4.3s to 2.9s per document.

**`nvidia/Nemotron-3.5-Super-VL` — the extraction path.** The redacted-text benchmark came back with **3 documents in 20 reporting "could not parse a JSON object from: ''"** — an empty response. It read like a parsing bug in the harness. It was not: Super-VL is a **reasoning model**, and it spends its completion tokens on `reasoning_content` before it writes an answer. On one measured call, 847 of 859 completion tokens went to the reasoning and 12 to the answer. With `max_tokens: 1024` the thinking had used the entire budget before the answer was written.
**Cost if unresolved:** a 15% silent failure rate in the extraction path, and — worse — the *shape* of the failure. An empty response looks like "the model could not do this task", when the truth is "the model did the task and was not allowed to finish talking". The Phase 2 leak test would have scored that as a detection failure, which is a completely different conclusion.
**Detection:** the error said "could not parse a JSON object from: ''" — an empty string, not malformed JSON. An empty response is a different failure from a malformed one.
**Fix:** the completion budget is now 4096, which covers the thinking and the answer, and the error message now names the finish reason and the reasoning-token count so the next reader can tell "ran out of budget" from "could not do it". 20/20 documents now parse, 100% correct, zero hallucinations.

**The general lesson:** on a reasoning model, `max_tokens` is a budget for the thinking, not for the answer. And a benchmark must treat an empty response as an *error*, never as a score.

### F-026 · A wrong API key is indistinguishable from a missing one
**Owner:** Person A · **Phase:** 0
Token Factory answers `401 {"detail":"Couldn't authenticate. Reason:
Unable authenticate"}` for a missing key, an invalid key, a revoked key,
and a key belonging to a different Nebius product. The responses are
byte-identical, so the error tells you nothing about which of those it is.
**Cost if unresolved:** a wrong key reads as "the API is down", and the
team debugs the service instead of the credential.
**Detection:** send the key and compare against a no-auth baseline — if
they are identical, the key is being rejected, not ignored.
**Fix:** regenerate the key from the Token Factory console
(`tokenfactory.nebius.com` → project → API keys). Note that Token Factory
keys and Nebius AI Cloud keys are not interchangeable, and the AI Cloud
hostnames do not resolve from every environment.

### F-027 · The Docker driver overrides the image's ENTRYPOINT
**Owner:** Person A · **Phase:** 0
The sandbox's canonical main process is the command after `--`, run
verbatim. The image's own `ENTRYPOINT` is not used. An image that relies on
its entrypoint to start the agent will not start the agent.
**Cost if unresolved:** the sandbox starts and immediately exits, which
looks like a crash in the agent rather than a contract mismatch.
**Detection:** compare the command in `sandbox create` with the image's
`ENTRYPOINT`.
**Fix:** name the program explicitly —
`-- /usr/local/bin/hermes-direct chat`. The helper script is unchanged;
only the invocation was wrong.
**Status:** fixed.

### F-028 · `--driver-config-json` is disabled by default in 0.1.4
**Owner:** Person A · **Phase:** 0
Attaching the persistent Hermes volume through driver config fails with
*"caller driver config is disabled; a gateway administrator must enable
allow_driver_config"*. The switch lives in the **driver** table
(`[openshell.drivers.docker]`), not in `[openshell.gateway]` — and the
gateway rejects unknown keys in both places, so putting it in the wrong
table makes the gateway refuse to start outright.
**Cost if unresolved:** the volume cannot be attached, so the agent loses
its state on every sandbox recreation, and the failure message points at
an administrator setting rather than at the command.
**Detection:** the error text names the setting.
**Fix:** `allow_driver_config = true` under `[openshell.drivers.docker]`.
**Status:** fixed.

### F-029 · External resources are admitted by label, and the workspace label must resolve
**Owner:** Person A · **Phase:** 0
A Docker volume attached through driver config is refused with *"external
resource not admitted by required labels"* unless it carries both
`openshell.ai/sandbox-attachable=true` and
`openshell.ai/sandbox-attachable-workspace=${workspace}`.
**Cost if unresolved:** the obvious workaround — turning resource admission
off — would let any caller attach any host volume to any sandbox. That
converts a labelling mistake into a sandbox-escape primitive.
**Detection:** the error names the admission rule.
**Fix:** keep admission enabled and label the volume. The gateway side
declares the required labels explicitly in `sandbox/gateway.toml` so the
approval contract is visible next to the volume definition.
**Status:** fixed.

### F-030 · A remote OpenShell CLI cannot authenticate without the gateway's client bundle
**Owner:** Person A · **Phase:** 0
The gateway requires mTLS. `openshell gateway add --local` does **not**
fetch a client certificate from a reachable gateway — it reuses local
material, so a client whose bundle was issued by a different CA fails with
`invalid peer certificate: BadSignature` even though the gateway is
reachable and healthy.
**Cost if unresolved:** a verifier run from another machine reports a dozen
of failures that look like broken infrastructure and are actually a
client-side credential problem. On a shared network namespace the gateway
is reachable at `127.0.0.1` and still unusable.
**Detection:** compare the error against a no-auth baseline; the gateway
answers identically for a missing and an invalid client certificate.
**Fix:** run the verifier on the gateway host, or copy the bundle
(`~/.config/openshell/gateways/<name>/mtls/`) to the client and register
with `--gateway <name>`.
**Status:** worked around — the verifier now reports unevaluable checks as
SKIP with the remedy, instead of as failures.

### F-031 · A verifier that cannot tell "not configured" from "cannot see"
**Owner:** Person A · **Phase:** 0
The first version of `sandbox/verify.sh` ran every check unconditionally.
With an unreachable gateway it reported "endpoint MISSING from the
effective policy", "no Token Factory connection in the last 15m" and
"model call failed" — twelve failures describing infrastructure that was
working. It also checked for a **local** Docker daemon, which is a
different question from "the gateway's compute driver is alive", and which
is false for any client that is not the gateway host.
**Cost if unresolved:** the artifact whose entire job is to certify a phase
would report a healthy environment as broken. Nobody trusts a verifier
that cries wolf.
**Detection:** run it from a machine that cannot reach the gateway.
**Fix:** fail fast on a transport failure, report the dependent checks as
SKIP, check the compute driver from the gateway's side, and print the
remedy. Three states now: PASS, FAIL, SKIP.
**Status:** fixed.

### F-032 · Two Phase 0 benchmark runs disagree about which model reads documents
**Owner:** Person A + Person B · **Phase:** 0/1 · **Status:** open
Two independent runs of `make ocr-bench`, over different corpora and with different candidate sets, name different winners. The 20-document run over five vision models scored `nvidia/Nemotron-3.5-Super-VL` at 99.0% field recovery and made it the OCR model of record (`docs/ocr-decision.md`). The 30-rendered-scan run scored `openbmb/MiniCPM-V-4_5` at 98.0% against Super-VL's 96.0%, at 2.5× less per document, and named MiniCPM instead. The two runs are not contradictory measurements — MiniCPM scores at or above Super-VL in both — but they disagree on the winner, and the choice is a product decision, not a merge detail: Super-VL is a Super-tier model, so using it as the document reader is exactly what forces the "extract locally, redact locally" architecture that was validated against it.
**Cost if unresolved:** Phase 1 builds the ingestion path on one model while the pinned `.env`, the decision record and the architecture document disagree about which one it is — and the Phase 2 router's redact target was chosen to match the reader.
**Detection:** reading the OCR decision in `docs/phase0-runbook.md` §8 — it records two runs with two winners.
**Fix:** Super-VL is the decision of record: it is what `eval/pin_models.py` pins, what `docs/ocr-decision.md` records, and what the Phase 2 router architecture was validated against. MiniCPM stays in the benchmark's candidate list, so the comparison stays reproducible, and its measurement is kept. Revisiting the choice is the first item carried into Phase 1; re-deciding it means re-validating the privacy architecture against a different reader.

### F-033 · The whole Token Factory domain stopped accepting connections
**Owner:** Person B · **Phase:** 0 · **Status:** fixed — recovered on its own
After roughly 250 API calls across three benchmark runs in a single session, `api.tokenfactory.nebius.com:443` stopped accepting TCP connections. DNS still resolved; the connect itself hung until it timed out. The whole `tokenfactory.nebius.com` domain was affected, not just the API path, and `github.com` and `pypi.org` were reachable at the same moment, so it was not the host's general connectivity.
**Cost if unresolved:** the live benchmarks cannot be re-run. This is F-007 arriving early and in a different shape: F-007 predicted a *rate limit* (a 429 with headers telling you to slow down); what happened was a *connection refusal* with nothing to read, which is much harder to diagnose from the error alone.
**Detection:** the redacted-text bench reported "timed out" on every document. A single `curl -v` showed `Trying 213.239.161.19:443...` and then nothing.
**Fix:** it recovered on its own within a day, with no change on this side, which is consistent with an IP-level block expiring rather than a configuration error. The Option C validation had already been run and committed before the outage, so the decision was never at risk. The lesson stands: **batch the benchmarks rather than running them back to back**, and keep the raw results committed so a decision never depends on being able to re-run it.

### F-034 · The field-recovery metric was too forgiving, and it hid a dropped line
**Owner:** Person B · **Phase:** 1 · **Status:** fixed
The OCR benchmark scored the local extractor at 93% field recovery, and the pipeline then discovered that the extractor was **dropping whole lines** from the documents — a line carrying the renewal date and the amount simply came back absent, and the field score did not say so. The metric compares normalised text, so `Registeredaddress:1003BraddockBoulevard` still scored as a perfect address. It was measuring the right fields and hiding the wrong failure.
**Cost if unresolved:** the local path looked fit for the pipeline when it was not, and the failure would have shown up as "the agent sometimes misses the amount" rather than "the OCR is losing lines".
**Detection:** building the pipeline on top of the extractor. A document whose amount was missing turned out to be a document whose *line* was missing.
**Fix:** a **line-coverage** metric alongside field recovery — the fraction of the source document's lines that survive the extraction. The local extractor scores 96% of fields and 85.5% of lines, and that gap is now visible instead of hidden.

### F-035 · OCR damage has shapes, and two of them are repairable
**Owner:** Person B · **Phase:** 1 · **Status:** worked around
The extractor returns the text it guessed, and the guesses have shapes: the space after a colon or a comma is dropped (`Accountnumber:`), two words run together (`1003BraddockBoulevard`), a lowercase word loses its space (`Dateof expiry:`), and a character is misread (`Office` comes back as `0ffice`).
The first three break every label-based parser, so a redactor and a deadline detector that need `Account number:` simply do not work on a scanned document.
**Cost if unresolved:** the pipeline works on clean text and silently fails on real scans, which is the only kind that matters.
**Detection:** the extracted text, read next to the source.
**Fix:** `agent/ingestion/ocr_text.py` repairs the damage that has a shape — inserting the missing spaces, splitting run-together words, and rejoining the known document labels. The repair runs *before* the redaction, because a redactor that could not find the label has already failed. The misread character is not repairable and is not pretended to be: that is what the model-assisted extraction is for.

### F-036 · A notice can carry more than one identifier
**Owner:** Person B · **Phase:** 1 · **Status:** fixed
An insurance renewal carries **both** a policy number (`POL-482913-Z`) and an account number (`1234-5678`). The first redaction of the account category was the policy number, and the first of the two was whichever the redactor reached first — so the draft quoted the account number to an insurer who wanted the policy number. A letter quoting the wrong identifier is worse than no letter.
**Cost if unresolved:** the agent sends a real letter to a real company with the wrong reference in it.
**Detection:** the test asserted the reference was the account number and the pipeline produced the policy number.
**Fix:** the label in front of the identifier decides which one to quote, and the label is still in the redacted text next to the placeholder. The policy number wins over the account number, a membership number over both.

### F-037 · The corpus was rendered at 96 DPI, and it was the benchmark's biggest defect
**Owner:** Person B · **Phase:** 0/1 · **Status:** fixed
With tesseract finally installed (F-020), the local extractor scored **84%** field recovery and **55% on renewal dates** — worse than the pip-only fallback, and implausible. The cause was not the OCR: the corpus rendered the documents at 15px monospace on a 1000px page, about **96 DPI**, and at that size the digit `0` is genuinely indistinguishable from an `8`. Tesseract read `2027-03-07` as `2827-83-07` and `2027-10-07` as `2627-18-87`. A real scan of a letter is 150–300 DPI, and a real document is legible.
Every Phase 0 number was measured on that corpus, so every one of them was distorted: the local fallback looked far worse than it is, the vision models looked far better than they are, and the "which model reads documents" decision was made on a document nobody would ever receive.
**Cost if unresolved:** the whole benchmark answers a question nobody asked. And the pipeline inherited it: with the old rendering, the reference number was missing from half the documents, the provider was wrong, the amount was missing, and the deadline kind was unknown.
**Detection:** tesseract scoring *worse* than a pip-only fallback, and the `0`→`8` pattern across every date. A model-independent failure is a harness failure — the same shape as F-011.
**Fix:** the corpus renders an A4 page at ~150 DPI with a 10pt body — what a scan or a phone photo gives. With that, tesseract scores **100% field recovery and 100% line coverage**, and the pipeline extracts the provider, the kind, the reference and the amount correctly on every document. The lesson is worth more than the fix: **a benchmark is only as representative as its fixtures, and a fixture that is harder than reality measures the fixture.**

### Pre-registered risks, now closed

| Ref | Was | Now |
|---|---|---|
| F-002 | Model ID casing is inconsistent across Nebius's own docs | **Confirmed and worse than expected** — see F-017 and F-018. Every ID is now read from the live catalog. |
| F-007 | Rate limits will bite during the leak test | **Not yet hit.** A 20-document, 5-model benchmark (100 calls) ran without a throttle. The 200-document, two-condition leak test is 10× that — still to be proven. |

_(F-001…F-031 above are the Phase 0 record. Later phases append here.)_

---

## Template

```
### F-NNN · Short title
**Owner:** Person A/B · **Phase:** N
What broke, in one or two sentences.
**Cost if unresolved:** time or scope.
**Detection:** how we noticed.
**Fix:** what we did.
**Status:** open / fixed / worked around
```
