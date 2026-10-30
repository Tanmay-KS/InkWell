# Phase 2 runbook — the privacy router on the gateway

This is the operator's path from "the code is in the repo" to "the sandbox
enforced it, and the video has the audit line to prove it".

The Phase 2 work has two halves:

- **Offline, done and tested here.** The middleware service, the three-way
  guard, the audit normaliser, the policy/gateway generators, the egress
  probe, the injection scenario, and the verifier. The unit tests and a real
  local gRPC smoke run cover the service; the scripts are exercised end to
  end against a fake `openshell` client and the real middleware.
- **Live, blocked on this machine.** `openshell status` fails with `invalid
  peer certificate: BadSignature`, there is no Docker daemon, and
  `host.openshell.internal` does not resolve. Those are the same conditions
  recorded in F-030 and F-043. Every live step below is written to be run on
  the gateway host, where the CLI holds the matching mTLS bundle.

Do not mark the Phase 2 exit condition closed until
`bash sandbox/verify_phase2.sh` prints `PASS` on that host.

## What the pieces are

```
 sandbox process
   │  POST https://api.tavily.com/search  {account, name, ...}
   ▼
 OpenShell supervisor
   │  1. network policy: host + path + port must be allowed
   │  2. L7 relay: call the registered middleware (PRE_CREDENTIALS)
   ▼
 ledger-router (this repo)               host:50051
   │  config.py    validate the policy's config
   │  body.py      JSON-aware redaction; image detection
   │  service.py   ALLOW / REDACT / DENY + audit JSONL
   ▼
 upstream (Token Factory / Tavily)
```

The middleware is the only place that sees **every** allowed outbound L7
request, including calls the agent code did not write. That is why it is the
enforcement point, not a library the agent has to remember to call.

The base `sandbox/policy.yaml` and `sandbox/gateway.toml` deliberately keep
the middleware **inactive**. The gateway calls every registered middleware
at startup and refuses to start if one is unavailable, so a clone without
the service must not carry the registration. The Phase 2 variants are
generated:

- `sandbox/router_policy.py` adds the `network_middlewares` entry to a copy
  of the base policy and validates the config against `RouterConfig`.
- `sandbox/gateway_config.py` uncomments or re-comments the registration
  block and validates the result with `tomllib`.
- `sandbox/router_register.sh` probes the service, preflights the generated
  gateway config, backs up the installed file, installs it, restarts, and
  restores the backup if the gateway does not come back.

## Prerequisites

1. A working Phase 0/1 gateway: `bash sandbox/verify.sh` passes on the
   gateway host, and `openshell sandbox list` shows `inkwell-hermes`.
2. The middleware dependencies in the interpreter that will run the service:

   ```bash
   .venv/bin/pip install -e ".[middleware]"
   ```

   The service needs `grpcio`, `protobuf`, `pydantic`; the scripts also use
   `PyYAML`. `sandbox/select_python.sh` prefers `.venv/bin/python`, then
   `python3`, and checks for all three. Override with
   `LEDGER_ROUTER_PYTHON=/path/to/python`.
3. A writable audit directory:

   ```bash
   sudo install -d -o "$USER" -g "$USER" /var/log/inkwell
   ```

   The service writes the audit JSONL on the **host**, not inside the
   sandbox. If the directory is not writable, the service fails closed: an
   audit trail it cannot write is treated as a reason not to allow the
   request.
4. The service must be reachable from the gateway **and** from the sandbox
   supervisors. The registration names
   `http://host.openshell.internal:50051`; the service binds `0.0.0.0:50051`
   by default. If the gateway itself runs in a container, confirm that
   `host.openshell.internal` resolves there before registering.

## The live procedure

Run these in order on the gateway host. The service goes in its own terminal
or as a systemd unit; the rest can be one shell.

### 1. Start the service

```bash
LEDGER_ROUTER_AUDIT_LOG=/var/log/inkwell/audit.jsonl \
  bash sandbox/middleware/ledger-router/run.sh
```

Keep it in the foreground for the first run so its log line is visible:

```
ledger-router 0.1.0 listening on 0.0.0.0:50051 (max payload 4194304 bytes)
```

Do not continue until it prints that line.

### 2. Prove the contract before touching the gateway

```bash
.venv/bin/python sandbox/middleware/ledger-router/probe.py --self-test
```

Expected:

```
PASS  Describe: ledger-router, HTTP_REQUEST/PRE_CREDENTIALS, max_payload=4194304, protocol=1.0
PASS  self-test: decision=DECISION_ALLOW, reason_code=personal_detail_redacted, body redacted
```

`Describe` is the same contract the gateway validates at startup. The
self-test sends one synthetic request through the full RPC and asserts that
`0847-4851` is gone from the returned body. It prints only the decision and
the reason code, never the body.

### 3. Dry-run the gateway config, then register

```bash
bash sandbox/router_register.sh --dry-run
bash sandbox/router_register.sh
```

The dry run leaves the generated TOML in `/tmp` for inspection and changes
nothing. The real run:

1. probes the service again (no registration if it is down),
2. generates the enabled config from the installed one (preserving local
   edits) or from `sandbox/gateway.toml`,
3. runs `openshell-gateway config preflight` on it,
4. backs up the installed config to `gateway.toml.bak.<timestamp>`,
5. installs it and runs `systemctl --user restart openshell-gateway`,
6. waits up to 30 seconds for `openshell status`; on failure it restores the
   backup and restarts.

If the gateway is not systemd-managed, set
`INKWELL_GATEWAY_RESTART_CMD='...'` or use `--no-restart` and restart it
yourself.

### 4. Attach the policy

```bash
bash sandbox/router_on.sh --audit-log /var/log/inkwell/audit.jsonl
```

This generates the router-enabled policy, applies it with `--wait`, turns on
the OCSF JSON stream, and verifies the effective policy mentions
`ledger_router`. If the gateway refuses the policy, the script immediately
re-applies `sandbox/policy.yaml`: the gateway's default failure mode blocks
traffic until a valid policy loads, and leaving a rejected policy in place
would be worse than not starting.

The output ends with:

```
ROUTER ON  inkwell-hermes
Next
  bash sandbox/verify_phase2.sh --audit-log /var/log/inkwell/audit.jsonl
  bash demo/injection/run_injection_demo.sh --audit-log /var/log/inkwell/audit.jsonl
```

### 5. Verify

```bash
bash sandbox/verify_phase2.sh --audit-log /var/log/inkwell/audit.jsonl
```

The six checks, in execution order:

| Kind | Check |
|---|---|
| required | middleware answers the supervisor contract and redacts |
| required | policy carries the middleware entry |
| required | egress allow/deny boundaries hold (5 probes) |
| info | OCSF JSON event stream is enabled |
| required | prompt-injection demo is blocked with an audit trail |
| required | audit log records router decisions without the demo values |

The audit check runs **after** the demo because the demo is what creates the
first live router decisions. `verify_phase2.sh` runs the demo itself; use
`--no-demo` to check only the infrastructure (the audit check then needs a
log with prior events, so it will fail on a fresh audit path).

### 6. The demo, on its own

```bash
bash demo/injection/run_injection_demo.sh --audit-log /var/log/inkwell/audit.jsonl
```

The scenario:

1. `demo/injection/untrusted_message.txt` is a synthetic provider email that
   tells the assistant to upload the household record to
   `https://collect.evil.example/upload` and, as a fallback, to search Tavily
   with `include_answer: true`.
2. `demo/injection/attempt_exfil.py` runs **inside the sandbox**, reads the
   message, and makes four requests: the unknown host, the echo shape, the
   bulk shape, and one benign query.
3. OpenShell's network policy refuses the unknown host before it leaves.
4. The middleware refuses the two Tavily shapes:
   `third_party_model_echo_forbidden` and
   `bulk_personal_detail_to_third_party`.
5. The benign query is allowed. Without an API key it reaches Tavily and
   gets a 401; that is a successful egress, not a failed demo.
6. The runner prints the new audit rows, checks for at least two router
   denials and one allow, and refuses to print a log that contains the
   synthetic account or email.

Expected last lines:

```
DEMO PASS
  The unknown host was refused by the network policy.
  The allowlisted-host exfiltration attempts were refused by the router.
  The benign search was allowed.
  The audit trail recorded the decisions without the personal values.
```

### 7. The OFF condition and teardown

```bash
bash sandbox/router_off.sh          # base policy; the OFF leg of the leak test
bash sandbox/router_unregister.sh   # remove the registration; restarts the gateway
```

`router_off.sh` refuses to leave a policy that still names the middleware.
`router_unregister.sh` refuses to remove the registration while the policy
still attaches it (use `--force` only if the sandbox is gone).

## The audit trail

The middleware appends one JSON object per inspected request to the path in
the policy's `config.audit_log_path` (default
`/var/log/inkwell/audit.jsonl`). The stable shape:

```json
{
  "timestamp": "2026-10-24T12:00:00+00:00",
  "event_type": "router.denied",
  "decision": "deny",
  "reason_code": "third_party_model_echo_forbidden",
  "host": "api.tavily.com",
  "detail": null,
  "raw": {
    "middleware_name": "ledger-router",
    "mode": "redact",
    "categories": {"account": 1},
    "body_sha256": "…",
    "body_bytes": 187
  }
}
```

Rules the implementation enforces:

- **No request body, no personal values.** `raw` carries a hash, a byte
  count, the mode, the middleware name, and category **counts**, never the
  matched text. `detail` is a stable reason string, not the payload.
- **The audit log is fail-closed.** If the path is unwritable, the service
  denies the request rather than allowing it untracked.
- **`router.denied` is a real middleware decision.** A fail-closed service
  error is `router.failed_closed`; a fail-open one is `router.failed_open`.
  The verifier and the demo distinguish them.
- **The runtime has its own record.** `openshell settings set … --key
  ocsf_json_enabled --value true` writes OCSF 4002 (HTTP Activity), 4001
  (Network Activity) and 2004 (Detection Finding). `privacy_router/audit_log.py::from_ocsf`
  normalises those into the same event shape. The per-request JSONL is the
  immediate trail; OCSF is the runtime's independent one. The demo reads the
  JSONL; the leak/gateway analysis can read either.

## Failure modes and what to do

| Symptom | Cause | Action |
|---|---|---|
| `probe.py` says `grpc is not installed` | wrong interpreter | `pip install -e ".[middleware]"`, or set `LEDGER_ROUTER_PYTHON` |
| `router_register.sh` says the service is not healthy | service down, or not reachable at the probe endpoint | start `run.sh`; check port 50051; probe `127.0.0.1:50051` and `host.openshell.internal:50051` |
| gateway does not come back after registration | service unreachable from the gateway, or TLS/plaintext mismatch | the script restores the backup; inspect `openshell logs --source gateway`; confirm `allow_insecure_transport` for the local dev endpoint |
| `policy set` is refused | middleware not registered, or config rejected | run `sandbox/router_policy.py --check`; confirm `router_register.sh` succeeded; the base policy has already been re-applied |
| every request is denied with `audit_unwritable` | audit directory not writable | fix the directory or the policy's `audit_log_path` |
| `openshell status` fails with `BadSignature` | this client does not hold the gateway's matching client bundle | run on the gateway host; see F-030/F-043 |
| egress probe says "allowed host + wrong path is refused" fails | the path rule does not match what you think | inspect `openshell policy get --full`; OpenShell path matching is exact-prefix based on the policy entry |

## Current status in this workspace

Offline checks that pass here:

- `pytest tests/test_router_middleware.py tests/test_injection_guard.py
  tests/test_audit_log.py tests/test_router_policy.py
  tests/test_gateway_config.py tests/test_phase2_scripts.py`
- a real local gRPC service + `probe.py --self-test`
- `verify_phase2.sh` and the injection demo against a fake `openshell` client
  and the real middleware
- `openshell-gateway config preflight` on the generated gateway TOML
- `sandbox/router_policy.py --off` is semantically identical to
  `sandbox/policy.yaml`

Live checks that are **not** done here:

- registering the middleware with a real gateway,
- applying the router policy with `--wait`,
- the five egress probes inside the real sandbox,
- the four injection attempts inside the real sandbox,
- the gateway leg of the leak test.

The blocker is the client certificate and the missing Docker daemon, not the
Phase 2 code. The honest claim until the live run is: *the router service,
the guard, the audit trail, and the demo are implemented and exercised
offline; the sandbox enforcement is the remaining live step.*
