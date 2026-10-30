"""WSGI front end for the public demo deployment.

The loopback server in `server.py` is the real interface: it binds to
`127.0.0.1`, refuses non-loopback `Host` headers, and reads the household
sidecar. Vercel cannot run that server, and exposing it would contradict
the privacy design.

This module reuses the exact same `LedgerHandler` and routes, but the
state it hands the handler is deliberately narrow:

* the database is a throwaway file in `/tmp`, seeded with the fictional
  demo fixtures — never the household sidecar;
* `public_demo` is true, which is the only reason the host check is
  bypassed;
* model drafting stays off, so no credential or Token Factory call is
  involved;
* every item, upload and cancellation lives for one warm instance and is
  lost when it is recycled.

That is the whole point: a stranger can click through the product without
touching anybody's household data.
"""

from __future__ import annotations

import email
import io
import os
import tempfile
import threading
from http import HTTPStatus
from pathlib import Path

from agent.memory.hermes_memory import HouseholdMemory
from interface.web.server import (
    LedgerHandler,
    RateLimiter,
    _demo_trace_state,
    _seed_demo,
)

__all__ = ["PUBLIC_DEMO", "app"]

PUBLIC_DEMO = (
    os.environ.get("LEDGER_PUBLIC_DEMO", "").strip().lower() in {"1", "true", "yes"}
    or bool(os.environ.get("VERCEL"))
)

_DB_PATH = Path(tempfile.mkdtemp(prefix="ledger-vercel-")) / "memory.db"
_SEED_LOCK = threading.Lock()
_SEEDED = False
_LOCAL = threading.local()


def _memory() -> HouseholdMemory:
    """One SQLite connection per thread, all onto the same throwaway file.

    `sqlite3` connections are not safe to share across threads. Vercel may
    serve concurrent requests in one instance, so each thread opens its own
    connection; WAL mode keeps them consistent.
    """
    memory = getattr(_LOCAL, "memory", None)
    if memory is not None:
        return memory
    memory = HouseholdMemory(_DB_PATH)
    memory.connect()
    _seed(memory)
    _LOCAL.memory = memory
    return memory


def _seed(memory: HouseholdMemory) -> None:
    global _SEEDED
    if _SEEDED:
        return
    with _SEED_LOCK:
        if not _SEEDED:
            _seed_demo(memory)
            _SEEDED = True


class _DemoState:
    """The `self.server` attributes `LedgerHandler` expects."""

    demo_mode = True
    server_port = 443

    def __init__(self) -> None:
        self.last_run: dict | None = None
        self.rate_limiter = RateLimiter.from_env()

    @property
    def public_demo(self) -> bool:
        return PUBLIC_DEMO

    @property
    def memory(self) -> HouseholdMemory:
        return _memory()

    @property
    def demo_trace(self) -> dict:
        cached = getattr(self, "_demo_trace", None)
        if cached is None:
            cached = _demo_trace_state().to_dict()
            self._demo_trace = cached
        return cached


_STATE = _DemoState()


class _CapturingHandler(LedgerHandler):
    """A handler that records the response instead of writing a socket."""

    def send_response_only(self, code: int, message: str | None = None) -> None:
        del message
        self._status = int(code)

    def send_header(self, keyword: str, value: str) -> None:
        self._headers.append((str(keyword), str(value)))

    def end_headers(self) -> None:
        return None


def _headers(environ: dict) -> email.message.Message:
    message = email.message.Message()
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            message[key[5:].replace("_", "-").title()] = str(value)
    if environ.get("CONTENT_TYPE"):
        message["Content-Type"] = environ["CONTENT_TYPE"]
    if environ.get("CONTENT_LENGTH"):
        message["Content-Length"] = environ["CONTENT_LENGTH"]
    return message


def _handler(environ: dict) -> _CapturingHandler:
    method = str(environ.get("REQUEST_METHOD") or "GET").upper()
    path = str(environ.get("PATH_INFO") or "/")
    query = str(environ.get("QUERY_STRING") or "")
    if query:
        path = f"{path}?{query}"

    handler = _CapturingHandler.__new__(_CapturingHandler)
    handler.command = method
    handler.path = path
    handler.request_version = str(environ.get("SERVER_PROTOCOL") or "HTTP/1.1")
    handler.requestline = f"{method} {path} {handler.request_version}"
    handler.headers = _headers(environ)
    handler.rfile = environ["wsgi.input"]
    handler.wfile = io.BytesIO()
    handler.client_address = (str(environ.get("REMOTE_ADDR") or "0.0.0.0"), 0)
    handler.server = _STATE
    handler.close_connection = False
    handler._status = 200
    handler._headers = []
    return handler


def app(environ: dict, start_response):
    """The WSGI callable Vercel loads from `app.py`."""
    method = str(environ.get("REQUEST_METHOD") or "GET").upper()
    handler = _handler(environ)

    if method in {"GET", "HEAD"}:
        handler.do_GET()
    elif method == "POST":
        handler.do_POST()
    else:
        handler.send_error(405)

    body = handler.wfile.getvalue()
    if method == "HEAD":
        body = b""

    status = int(handler._status)
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Unknown"
    headers = handler._headers
    if not any(name.lower() == "content-length" for name, _ in headers):
        headers.append(("Content-Length", str(len(body))))

    start_response(f"{status} {reason}", headers)
    return [body]
