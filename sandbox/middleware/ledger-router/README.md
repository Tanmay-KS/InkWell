# ledger-router — the Ledger privacy router (Phase 2)

This directory will hold the OpenShell **supervisor middleware** service that
implements the project's differentiator: it inspects every outbound request
the sandbox makes, strips personal details before the request leaves, blocks
prompt-injection exfiltration attempts, and writes an audit trail.

Status: **not started.** Phase 2. The Phase 0 and Phase 1 scaffolds exist so
the shape of the system is fixed before it is built.

---

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
is forwarded, and again on responses. It therefore sees *all* allowed egress,
not just the paths we wrote.

The OpenShell docs describe exactly this use case: *"Redact API tokens from
outgoing request bodies"*, *"Reject requests that contain content your
organization prohibits"*, *"Remove sensitive content from HTTP responses
before the agent receives them."*

## How it plugs into OpenShell

**1. Implement the gRPC service.** The contract is
`proto/supervisor_middleware.proto` in the NVIDIA/OpenShell repository
(Apache-2.0). The reference implementation is Rust:
`examples/supervisor-middleware-content-guard`. We will write the service in
Python and generate the stubs from the `.proto` with `grpcio-tools`.

**2. Register it with the gateway.** In `sandbox/gateway.toml`:

```toml
[[openshell.supervisor.middleware]]
name = "ledger-router"
grpc_endpoint = "http://host.openshell.internal:50051"
allow_insecure_transport = true
max_payload_bytes = 262144
timeout = "500ms"
```

The service must be reachable from **both** the gateway host and the sandbox
supervisors. `host.openshell.internal` is the gateway host as seen from
inside a container.

> The gateway calls every registered service at startup and **will not start**
> if one is unavailable or protocol-incompatible. Register the router only
> once the service is up, and give it a container restart policy.

**3. Attach it in the sandbox policy.** In `sandbox/policy.yaml`, under
`network_middlewares` (the block is already written there, commented out).

Middleware is selected by destination host, so one entry covers both the
inference endpoint and Tavily. The router distinguishes them by inspecting
the request body — the Token Factory body carries `"model"`, the Tavily body
carries `"query"`.

## Contract sketch

| Operation | Phase | Purpose |
|---|---|---|
| `HTTP_REQUEST` / `PRE_CREDENTIALS` | request | redact the body before it leaves; deny it outright if it looks like an exfiltration attempt |
| `HTTP_RESPONSE` / `PRE_RETURN` | response | strip personal details out of what comes back |
| `WEBSOCKET_MESSAGE` / `PRE_CREDENTIALS` | request | not needed for the MVP |

The request body is inspected *before* OpenShell injects provider
credentials, so the router never sees an API key.

## What it will call

`privacy_router/redact.py` — the redaction library. It runs a deterministic
local pass first (regex/NER for account numbers, emails, phone numbers,
addresses, names), which costs nothing and keeps most personal details
*inside* the sandbox. Only spans the local pass cannot confidently classify
are escalated to a Nemotron Nano call for semantic redaction.

That ordering matters for the leak test: the Nano call is itself outbound
traffic, so the honest claim is "personal details never reach a Super/Ultra
tier or a third-party service", with the Nano redaction call reported as a
separate line item rather than hidden.

## What it will not do

* It will not inspect WebSocket binary frames, messages the server sends to
  the agent, compressed bodies, or endpoints marked `tls: skip`. OpenShell
  cannot inspect those today, and the middleware inherits that limit.
* It will not parse general PII out of arbitrary prose. The local pass
  targets structured identifiers; the Nano pass handles context.

## Files

```
sandbox/middleware/ledger-router/
├── README.md              ← this file
├── proto/                 ← vendored copy of supervisor_middleware.proto
├── service.py             ← the gRPC server
├── config.py              ← config schema + validation (ValidateConfig)
└── requirements.txt
```

## Related

- `privacy_router/redact.py` — the redaction library this calls
- `privacy_router/injection_guard.py` — the injection/exfiltration decision
- `privacy_router/audit_log.py` — normalizes OpenShell's OCSF events into the
  project's audit trail
- `eval/leak_test.py` — the ON/OFF measurement
