#!/usr/bin/env python3
"""Check that the InkWell privacy router middleware is live and sane.

Run this on the gateway host before registering the service, and again after
starting it:

    python3 sandbox/middleware/ledger-router/probe.py
    python3 sandbox/middleware/ledger-router/probe.py --endpoint 127.0.0.1:50051 --self-test

`Describe` proves the contract the gateway will validate at startup: the
service name, the HTTP/PRE_CREDENTIALS binding, protocol major 1, and the
capability marker. `--self-test` also sends one synthetic request through
the full RPC and asserts that the returned body no longer carries the
account number. The probe never prints the body; it prints only the
decision and the redacted marker presence.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
for path in (SCRIPT_DIR, SCRIPT_DIR / "generated"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import grpc
    from google.protobuf import json_format, struct_pb2
except ImportError as exc:  # pragma: no cover - operator-facing
    print(
        "grpc is not installed in this Python. Install the middleware extra:\n"
        "  .venv/bin/pip install -e '.[middleware]'",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

import supervisor_middleware_pb2 as pb
import supervisor_middleware_pb2_grpc as pb_grpc

CONTRACT_CAPABILITY = "openshell.supervisor-middleware.contract"


def as_struct(value: dict) -> struct_pb2.Struct:
    return json_format.ParseDict(value, struct_pb2.Struct())


def probe(endpoint: str, timeout: float, self_test: bool) -> int:
    channel = grpc.insecure_channel(endpoint)
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
    except grpc.FutureTimeoutError:
        print(f"FAIL  middleware not reachable at {endpoint}", file=sys.stderr)
        return 1

    stub = pb_grpc.SupervisorMiddlewareStub(channel)
    try:
        manifest = stub.Describe(pb.MiddlewareDescribeRequest(), timeout=timeout)
    except grpc.RpcError as exc:
        print(f"FAIL  Describe failed: {exc.code().name}", file=sys.stderr)
        return 1

    problems: list[str] = []
    if manifest.name != "ledger-router":
        problems.append(f"service name is {manifest.name!r}, expected 'ledger-router'")
    operations = {
        (binding.operation, binding.phase) for binding in manifest.bindings
    }
    expected = (
        pb.SUPERVISOR_MIDDLEWARE_OPERATION_HTTP_REQUEST,
        pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
    )
    if expected not in operations:
        problems.append("the HTTP_REQUEST/PRE_CREDENTIALS binding is missing")
    if manifest.extension.protocol_version.major != 1:
        problems.append(
            f"protocol major is {manifest.extension.protocol_version.major}, expected 1"
        )
    if CONTRACT_CAPABILITY not in manifest.extension.supported_capabilities:
        problems.append("the supervisor-middleware contract capability is missing")

    if problems:
        for problem in problems:
            print(f"FAIL  {problem}", file=sys.stderr)
        return 1

    binding = next(
        binding
        for binding in manifest.bindings
        if (binding.operation, binding.phase) == expected
    )
    print(
        "PASS  Describe: ledger-router, "
        f"HTTP_REQUEST/PRE_CREDENTIALS, max_payload={binding.max_payload_bytes}, "
        f"protocol={manifest.extension.protocol_version.major}"
        f".{manifest.extension.protocol_version.minor}"
    )

    if not self_test:
        return 0

    with tempfile.NamedTemporaryFile(
        prefix="ledger-router-probe-", suffix=".jsonl", delete=False
    ) as handle:
        audit_path = handle.name
    config = {
        "mode": "redact",
        "redact_models": ["nvidia/Nemotron-3.5-Super-VL"],
        "redact_hosts": ["api.tavily.com"],
        "allowed_hosts": [
            "api.tokenfactory.nebius.com",
            "api.tavily.com",
        ],
        "ocr_models": ["nvidia/Nemotron-3.5-Super-VL"],
        "deny_threshold": 3,
        "audit_log_path": audit_path,
    }
    request = pb.HttpRequestEvaluation(
        phase=pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
        context=pb.RequestContext(request_id="probe", sandbox="probe"),
        config=as_struct(config),
        target=pb.HttpRequestTarget(
            host="api.tokenfactory.nebius.com",
            port=443,
            method="POST",
            path="/v1/chat/completions",
        ),
        body=json.dumps(
            {
                "model": "nvidia/Nemotron-3.5-Super-VL",
                "messages": [
                    {
                        "role": "user",
                        "content": "Policy holder Devin Sullivan account 0847-4851",
                    }
                ],
            }
        ).encode(),
        middleware_name="ledger-router",
    )
    try:
        result = stub.EvaluateHttpRequest(request, timeout=timeout)
    except grpc.RpcError as exc:
        print(f"FAIL  self-test RPC failed: {exc.code().name}", file=sys.stderr)
        return 1
    finally:
        Path(audit_path).unlink(missing_ok=True)

    if result.decision != pb.DECISION_ALLOW or not result.has_body:
        print(
            f"FAIL  self-test expected a redacted allow, got "
            f"{pb.Decision.Name(result.decision)}",
            file=sys.stderr,
        )
        return 1
    body = json.loads(result.body)
    content = body["messages"][0]["content"]
    if "[ACCOUNT_1]" not in content or "0847-4851" in content:
        print("FAIL  self-test did not redact the account number", file=sys.stderr)
        return 1
    print(f"PASS  self-test: decision={pb.Decision.Name(result.decision)}, "
          f"reason_code={result.reason_code}, body redacted")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default="127.0.0.1:50051",
        help="gRPC endpoint (default: 127.0.0.1:50051)",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="send one synthetic request and assert the body is redacted",
    )
    args = parser.parse_args(argv)
    return probe(args.endpoint, args.timeout, args.self_test)


if __name__ == "__main__":
    raise SystemExit(main())
