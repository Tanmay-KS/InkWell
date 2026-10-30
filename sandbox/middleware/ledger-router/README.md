# ledger-router — the InkWell privacy router (Phase 2)

The OpenShell **supervisor middleware** that implements the project's
differentiator: it inspects every allowed outbound HTTP request, strips
personal details before the request leaves, blocks prompt-injection
exfiltration shapes, and writes an audit trail.

Status: **implemented; live gateway registration pending.** The service is
exercised over a real local gRPC channel by `tests/test_router_middleware.py`
and `probe.py --self-test`. The live run is blocked by the client-certificate
condition in F-043; see [`docs/phase2-runbook.md`](../../../docs/phase2-runbook.md).

## Why a supervisor middleware and not just a library

The plan originally put redaction in `privacy_router/redact.py` and called it
from our own code paths. That works for calls we make, but it cannot see:

* a model call Hermes makes internally on its own,
* a tool invocation that reaches the network without going through our
  `search/tavily_client.py`,
* an agent that decides, under prompt injection, to send data somewhere
  unexpected.

A supervisor middleware runs inside the OpenShell supervisor's request path.
The supervisor calls it after the network policy check and before the request
is forwarded. It therefore sees *all* allowed egress, not just the paths we
wrote.

## What is implemented

The service advertises exactly one binding:

| Operation | Phase | Purpose |
|---|---|---|
| `HTTP_REQUEST` | `PRE_CREDENTIALS` | inspect/redact the body before it leaves; deny exfiltration shapes |

The request body is inspected *before* OpenShell injects provider
credentials, so the router never sees an API key.

Response inspection (`HTTP_RESPONSE` / `PRE_RETURN`) and WebSocket message
inspection are **not** advertised. They are not needed for the leak test or
the injection demo, and a service that advertises a binding it does not
implement is worse than one that does not. Add them deliberately if the
product needs to redact responses.

## The decision

The guard (`privacy_router.injection_guard`) returns one of three outcomes
with a stable reason code:

| Decision | Meaning | Example reason codes |
|---|---|---|
| `ALLOW` | no personal detail, or only a shape the policy permits | `no_personal_detail` |
| `REDACT` | personal detail removed; the rewritten body is forwarded | `personal_detail_redacted` |
| `DENY` | the request must not leave | `third_party_model_echo_forbidden`, `bulk_personal_detail_to_third_party`, `binary_body_uninspectable`, `personal_detail_to_unexpected_destination` |

The deterministic local pass runs first. `escalate` defaults to `false`: the
Nano escalation is optional, and a gateway timeout that is shorter than a
model call must not turn into an unredacted request. If escalation is on and
it fails, the request is denied, not forwarded.

The service fails **closed** when:

* a body is binary on an inspected host,
* the guard detects a detail but no redaction was applied,
* the body parser raises,
* the audit log cannot be written.

The audit log is part of the enforcement, not a side effect: an allow that
cannot be recorded is not an allow.

## How it plugs into OpenShell

**1. Register it with the gateway.** The committed
`sandbox/gateway.toml` keeps the block commented out. The gateway calls every
registered service at startup and **will not start** if one is unavailable or
protocol-incompatible, so a clone without the service must not carry the
registration. Generate and install the enabled config with:

```bash
bash sandbox/router_register.sh --dry-run   # inspect + preflight only
bash sandbox/router_register.sh             # backs up, installs, restarts
```

The block it enables:

```toml
[[openshell.supervisor.middleware]]
name = "ledger-router"
grpc_endpoint = "http://host.openshell.internal:50051"
allow_insecure_transport = true
max_payload_bytes = 4194304
timeout = "1500ms"
```

`max_payload_bytes` matches the service's advertised 4 MiB (document images
are the one body that can grow). `1500ms` matches `RouterConfig.timeout_ms`.

**2. Attach it in the sandbox policy.** The base `sandbox/policy.yaml` keeps
`network_middlewares` commented out too. Generate the enabled policy and
apply it with `--wait`:

```bash
python3 sandbox/router_policy.py --check          # validate, no gateway
bash sandbox/router_on.sh                        # probe, generate, apply
```

Middleware is selected by destination host, so one entry covers both the
inference endpoint and Tavily. The router distinguishes them by inspecting
the request body — the Token Factory body carries `"model"`, the Tavily body
carries `"query"`.

If the policy is refused, `router_on.sh` immediately re-applies the base
policy. The gateway's default failure mode is fail-closed, so a rejected
policy would otherwise take the sandbox's egress down.

**3. Detach and unregister.** `sandbox/router_off.sh` applies the base policy
(the OFF condition). `sandbox/router_unregister.sh` removes the registration
and refuses while the policy still names it.

## Configuration

The policy block is validated by `ledger_router.config.RouterConfig`
(Pydantic, `extra="forbid"`). The defaults:

| Key | Default | Notes |
|---|---|---|
| `mode` | `redact` | `deny` refuses a request that would need redaction |
| `redact_models` | Super-VL, Ultra | Token Factory model IDs whose bodies are inspected |
| `redact_hosts` | `api.tavily.com` | hosts whose bodies are always inspected |
| `allowed_hosts` | Token Factory, Tavily | the middleware's own destination allowlist |
| `third_party_hosts` | `api.tavily.com` | bulk/echo rules apply here |
| `task_hosts` | `[]` | empty means every allowed host is task-appropriate |
| `deny_hosts` | `[]` | explicit denials |
| `forbidden_flags` | `include_answer`, `include_raw_content` | Tavily flags that ask for a model-written echo |
| `deny_threshold` | `3` | distinct personal-detail categories before a bulk deny |
| `escalate` | `false` | optional Nano call for residual spans |
| `timeout_ms` | `1500` | must be less than the gateway registration timeout |
| `audit_log_path` | `/var/log/inkwell/audit.jsonl` | host path, not a sandbox path |

An unknown key is rejected rather than ignored: a typo in
`deny_threshold` must not silently leave the default in place.

## The audit trail

One JSON object per inspected request, appended to `audit_log_path`:

```json
{
  "timestamp": "2026-10-24T12:00:00+00:00",
  "event_type": "router.denied",
  "decision": "deny",
  "reason_code": "third_party_model_echo_forbidden",
  "host": "api.tavily.com",
  "detail": "third-party model echo requested",
  "raw": {
    "path": "/search",
    "categories": ["account"],
    "body_sha256": "…",
    "duration_ms": 1.026,
    "escalated": false
  }
}
```

Event types are past tense and match the OCSF normaliser:
`router.allowed`, `router.redacted`, `router.denied` (plus the runtime's
`router.failed_open` / `router.failed_closed` in OCSF normalisation).

`raw` never contains the body, the matched values, or the query. Category
**names** say what kind of detail was present; the body hash and duration
say what happened. `sandbox/audit_tail.py` prints only the decision fields
and can fail a run if a forbidden value appears.

## Running it

```bash
# host terminal; binds 0.0.0.0:50051
LEDGER_ROUTER_AUDIT_LOG=/var/log/inkwell/audit.jsonl \
  bash sandbox/middleware/ledger-router/run.sh

# second terminal
.venv/bin/python sandbox/middleware/ledger-router/probe.py --self-test
```

`run.sh` sources `.env` for `NEBIUS_API_KEY` only when escalation is on; the
key is never printed or put on a command line. The default audit directory is
`/var/log/inkwell`; set `LEDGER_ROUTER_AUDIT_LOG` to a writable path for a
local run.

## Files

```
sandbox/middleware/ledger-router/
├── README.md
├── run.sh                  entry point (bind, audit path, worker count)
├── probe.py                Describe + synthetic redaction self-test
├── requirements.txt        grpcio, protobuf, pydantic, PyYAML
├── proto/                  vendored supervisor_middleware.proto + extension.proto
├── generated/              checked-in Python stubs
└── ledger_router/
    ├── __main__.py         argparse → serve()
    ├── config.py           RouterConfig, as_guard_policy
    ├── body.py             JSON-aware body parsing and redaction
    └── service.py          gRPC contract, decisions, audit
```

## Limits

* It sees HTTP bodies on the hosts the policy attaches it to. It does not see
  Telegram (the policy allows it, but the Phase 2 entry does not attach the
  router to it), WebSocket frames, compressed bodies, `tls: skip` endpoints,
  or traffic the network policy refuses before the middleware.
* It does not parse general PII out of arbitrary prose. The deterministic
  pass targets structured identifiers; the Nano pass handles context when
  explicitly enabled.
* It does not score the response. Tavily's results are third-party content;
  the router's job is the query.

## Related

- `privacy_router/redact.py` — the redaction library this calls
- `privacy_router/injection_guard.py` — the exfiltration decision
- `privacy_router/audit_log.py` — OCSF normaliser and JSONL append
- `sandbox/verify_phase2.sh` — contract, egress, audit, demo
- [`docs/phase2-runbook.md`](../../../docs/phase2-runbook.md) — live procedure
