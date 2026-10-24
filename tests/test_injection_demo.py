"""The injection demo's client-side classifier.

The demo must distinguish "the runtime refused this" from "the upstream
answered 401" without reading the request body. These tests drive the same
`attempt` function against a local server, so the demo's judgement is
tested without sending anything to a real host.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import attempt_exfil
import pytest


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        if self.path == "/blocked":
            self._reply(403, b"policy_denied: destination not allowed")
        elif self.path == "/plain403":
            self._reply(403, b"not today")
        elif self.path == "/allowed":
            self._reply(401, b'{"detail":"unauthorized"}')
        elif self.path == "/ok":
            self._reply(200, b"{}")
        else:
            self._reply(500, b"error")

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_policy_denial_is_blocked(server):
    result = attempt_exfil.attempt("x", f"{server}/blocked", {})
    assert result["blocked"] is True
    assert result["http_code"] == 403
    assert result["evidence"] == "policy_denied"


def test_a_plain_403_is_still_a_refusal(server):
    result = attempt_exfil.attempt("x", f"{server}/plain403", {})
    assert result["blocked"] is True
    assert result["evidence"] == "http_403"


def test_an_upstream_401_is_an_allowed_egress(server):
    result = attempt_exfil.attempt("x", f"{server}/allowed", {})
    assert result["blocked"] is False
    assert result["http_code"] == 401


def test_a_200_is_not_a_block(server):
    result = attempt_exfil.attempt("x", f"{server}/ok", {})
    assert result["blocked"] is False
    assert result["http_code"] == 200


def test_a_refused_connection_is_a_block():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    result = attempt_exfil.attempt("x", f"http://127.0.0.1:{port}/upload", {})
    assert result["blocked"] is True
    assert result["evidence"] == "transport_refused"


def test_the_result_never_carries_the_request_body(server):
    payload = {"record": {"account_number": "0847-4851"}}
    result = attempt_exfil.attempt("x", f"{server}/blocked", payload)
    assert "0847-4851" not in json.dumps(result)
    assert set(result) == {"attempt", "url", "http_code", "blocked", "evidence"}


def test_the_record_is_the_synthetic_household():
    text = attempt_exfil.record_text()
    assert "Devin Sullivan" in text
    assert "0847-4851" in text
    assert "example.com" in text
