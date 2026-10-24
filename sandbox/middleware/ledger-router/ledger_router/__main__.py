"""Run the Ledger privacy router middleware.

    python -m ledger_router --bind 0.0.0.0 --port 50051

The service must be reachable from both the gateway host and the sandbox
supervisors. Binding to `0.0.0.0` is deliberate; the gateway's registration
names the address it uses (`host.openshell.internal:50051` by default).
"""

from __future__ import annotations

import argparse
import signal
import threading

from ledger_router.config import RouterConfig
from ledger_router.service import _configure_logging, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ledger privacy router middleware")
    parser.add_argument("--bind", default="0.0.0.0", help="address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=50051, help="port (default: 50051)")
    parser.add_argument("--max-workers", type=int, default=8, help="gRPC worker threads")
    parser.add_argument(
        "--max-payload-bytes",
        type=int,
        default=4 * 1024 * 1024,
        help="largest request body advertised to the gateway",
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        help="default audit log path when the policy does not set one",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    default_config = RouterConfig()
    if args.audit_log:
        default_config = default_config.model_copy(update={"audit_log_path": args.audit_log})

    server = serve(
        bind=args.bind,
        port=args.port,
        max_workers=args.max_workers,
        max_payload_bytes=args.max_payload_bytes,
        default_config=default_config,
    )
    server.start()

    stop = threading.Event()

    def _shutdown(signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    stop.wait()
    server.stop(grace=3).wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
