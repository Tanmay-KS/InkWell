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

_(nothing yet — this is where we log what actually breaks)_

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
