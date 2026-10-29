"""Tests for the loopback web interface.

Two properties matter more than the rendering:

* the server is only reachable from loopback, so a demo cannot be exposed
  by accident and the household database cannot be fetched cross-origin;
* the demo view is seeded with fictional data and never reads the real
  household sidecar.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest

from agent.memory.hermes_memory import HouseholdMemory
from interface.web.server import (
    MAX_UPLOAD,
    LedgerHandler,
    RateLimiter,
    _seed_demo,
    serve,
)

NOTICE = """Meridian Mutual Insurance
Renewal Notice

Dear Taylor Reed,

Your homeowners policy is scheduled to renew on 2027-03-07.
Your current premium is $148.95.

Policy number: POL-482913-Z
Policy holder: Taylor Reed
"""


@pytest.fixture
def web(tmp_path):
    httpd = HTTPServer(("127.0.0.1", 0), LedgerHandler)
    httpd.daemon_threads = True
    ready = threading.Event()
    closed = threading.Event()

    def run() -> None:
        memory = HouseholdMemory(tmp_path / "memory.db")
        memory.connect()
        _seed_demo(memory)
        httpd.memory = memory  # type: ignore[attr-defined]
        httpd.demo_mode = True  # type: ignore[attr-defined]
        ready.set()
        try:
            httpd.serve_forever()
        finally:
            memory.close()
            closed.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5), "web server did not start"
    yield httpd
    httpd.shutdown()
    thread.join(5)
    httpd.server_close()
    assert closed.is_set()


def _call(httpd, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None):
    conn = HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read()
    status = response.status
    response_headers = dict(response.getheaders())
    conn.close()
    return status, response_headers, payload


def _json(httpd, method: str, path: str, payload: dict | bytes, headers: dict | None = None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    status, response_headers, raw = _call(
        httpd, method, path, body=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    return status, response_headers, json.loads(raw)


# --- the loopback guard ----------------------------------------------------


def test_a_foreign_host_header_is_refused(web):
    status, _, _ = _call(web, "GET", "/api/summary", headers={"Host": "evil.example"})
    assert status == 403


def test_a_cross_origin_request_is_refused(web):
    status, _, _ = _call(
        web, "GET", "/api/summary", headers={"Origin": "http://evil.example"}
    )
    assert status == 403


def test_a_same_origin_request_is_allowed(web):
    status, _, _ = _call(
        web, "GET", "/api/summary",
        headers={"Origin": f"http://127.0.0.1:{web.server_port}"},
    )
    assert status == 200


def test_serving_must_bind_to_loopback():
    with pytest.raises(ValueError, match="loopback"):
        serve("0.0.0.0", 0)


# --- static assets ---------------------------------------------------------


def test_the_page_is_served_with_security_headers(web):
    status, headers, body = _call(web, "GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert b"<h1>Ledger</h1>" in body


def test_the_javascript_is_served_as_javascript(web):
    status, headers, body = _call(web, "GET", "/app.js")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert b"/api/summary" in body


def test_path_traversal_is_refused(web):
    status, _, _ = _call(web, "GET", "/../pyproject.toml")
    assert status == 404
    status, _, _ = _call(web, "GET", "/%2e%2e/server.py")
    assert status == 404


def test_an_unknown_path_is_404(web):
    status, _, _ = _call(web, "GET", "/nothing-here")
    assert status == 404


# --- the demo data ---------------------------------------------------------


def test_the_demo_summary_is_fictional_and_seeded(web):
    status, _, data = _json(web, "GET", "/api/summary", {})
    assert status == 200
    assert data["demo"] is True
    assert len(data["items"]) == 5
    assert {item["provider"] for item in data["items"]} == {
        "Northstar Auto", "Orchard Fitness", "BrightSmile Dental", "RiverFlix",
    }
    # A cancelled item stays in the household list but not in "coming up".
    assert "RiverFlix" not in {item["provider"] for item in data["due"]}
    assert len(data["due"]) == 4
    # The demo opens on a pushed finding, not on a typed prompt.
    kinds = {finding["kind"] for finding in data["findings"]["findings"]}
    assert "duplicate_charge" in kinds
    assert "deadline" in kinds


# --- the chat route --------------------------------------------------------


def test_chat_answers_from_the_seeded_memory(web):
    status, _, data = _json(web, "POST", "/api/chat", {"text": "What's coming up?"})
    assert status == 200
    assert "Orchard Fitness" in data["reply"]
    assert "RiverFlix" not in data["reply"]


def test_chat_rejects_an_empty_message(web):
    status, _, data = _json(web, "POST", "/api/chat", {"text": ""})
    assert status == 400
    assert "error" in data


def test_chat_rejects_a_body_that_is_not_an_object(web):
    status, _, data = _json(web, "POST", "/api/chat", [1, 2, 3])
    assert status == 400
    assert "error" in data


def test_a_slash_command_through_the_web_route_changes_state(web):
    _, _, summary = _json(web, "GET", "/api/summary", {})
    orchard = next(item for item in summary["items"] if item["provider"] == "Orchard Fitness")

    status, _, data = _json(web, "POST", "/api/chat", {"text": f"/cancel {orchard['item_id']}"})
    assert status == 200
    assert "cancelled" in data["reply"]

    _, _, summary = _json(web, "GET", "/api/summary", {})
    updated = next(item for item in summary["items"] if item["item_id"] == orchard["item_id"])
    assert updated["state"] == "cancelled"
    assert orchard["item_id"] not in {item["item_id"] for item in summary["due"]}


# --- the status route ------------------------------------------------------


def test_status_update_is_recorded(web):
    _, _, summary = _json(web, "GET", "/api/summary", {})
    item = summary["items"][0]
    status, _, data = _json(
        web, "POST", "/api/status",
        {"item_id": item["item_id"], "status": "paused"},
    )
    assert status == 200
    assert data["item"]["state"] == "paused"


def test_an_unsupported_status_is_rejected(web):
    _, _, summary = _json(web, "GET", "/api/summary", {})
    item = summary["items"][0]
    status, _, data = _json(
        web, "POST", "/api/status",
        {"item_id": item["item_id"], "status": "deleted"},
    )
    assert status == 400
    assert "unsupported" in data["error"]


def test_an_unknown_item_is_404(web):
    status, _, data = _json(
        web, "POST", "/api/status", {"item_id": "item-nope", "status": "cancelled"}
    )
    assert status == 404
    assert "error" in data


# --- the upload route ------------------------------------------------------


def test_a_text_notice_is_tracked_and_drafted(web):
    status, _, data = _json(
        web, "POST", "/api/upload", NOTICE.encode(),
        headers={"X-Filename": "notice.txt", "Content-Type": "application/octet-stream"},
    )
    assert status == 200
    assert data["item"]["provider"] == "Meridian Mutual Insurance"
    assert data["deadline"] == "2027-03-07"
    assert data["draft"]

    _, _, summary = _json(web, "GET", "/api/summary", {})
    assert "Meridian Mutual Insurance" in {item["provider"] for item in summary["items"]}
    assert len(data["trace"]) >= 5
    assert any(step["agent"] == "Auditor" for step in data["trace"])
    assert data["audit"]["decision"] in {"approve", "reject"}


def test_the_demo_trace_contains_a_rejection_and_a_revision(web):
    from interface.web.server import _demo_trace_state

    web.demo_trace = _demo_trace_state().to_dict()  # type: ignore[attr-defined]
    status, _, data = _json(web, "GET", "/api/trace", {})
    assert status == 200
    statuses = [step["status"] for step in data["run"]["trace"] if step["agent"] == "Auditor"]
    assert "rejected" in statuses
    assert "ok" in statuses
    denied = [step for step in data["run"]["trace"] if step["egress"].startswith(("allowed", "blocked"))]
    assert denied == []


def test_the_trace_never_contains_raw_personal_detail(web):
    from interface.web.server import _demo_trace_state

    run = _demo_trace_state().to_dict()
    blob = json.dumps(run)
    assert "taylor.reed@example.com" not in blob
    assert "POL-482913-Z" not in blob
    for step in run["trace"]:
        assert "@" not in step["input_summary"]
        assert "@" not in step["output_summary"]


def test_an_upload_without_a_date_is_rejected(web):
    status, _, data = _json(
        web, "POST", "/api/upload", b"a note with no date at all",
        headers={"X-Filename": "note.txt", "Content-Type": "application/octet-stream"},
    )
    assert status == 422
    assert "error" in data


def test_an_upload_larger_than_the_limit_is_refused(web):
    conn = HTTPConnection("127.0.0.1", web.server_port, timeout=5)
    conn.putrequest("POST", "/api/upload")
    conn.putheader("Content-Length", str(MAX_UPLOAD + 1))
    conn.endheaders()
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    assert response.status == 413
    assert b"15 MiB" in payload


def test_an_invalid_content_length_is_a_client_error(web):
    conn = HTTPConnection("127.0.0.1", web.server_port, timeout=5)
    conn.putrequest("POST", "/api/chat")
    conn.putheader("Content-Length", "banana")
    conn.endheaders()
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 400


# --- rate limiting ---------------------------------------------------------


def test_the_limiter_allows_then_refuses_then_recovers():
    limiter = RateLimiter(limit=2, window_seconds=0.2)

    assert limiter.check("client") == (True, 0)
    assert limiter.check("client") == (True, 0)
    allowed, retry_after = limiter.check("client")
    assert allowed is False
    assert retry_after >= 1
    # A different client has its own window.
    assert limiter.check("other")[0] is True

    import time

    time.sleep(0.25)
    assert limiter.check("client")[0] is True


def test_a_post_endpoint_returns_429_with_a_retry_after(web):
    web.rate_limiter = RateLimiter(limit=2, window_seconds=60)  # type: ignore[attr-defined]

    for _ in range(2):
        status, _, _ = _json(web, "POST", "/api/chat", {"text": "What's coming up?"})
        assert status == 200

    status, headers, data = _json(web, "POST", "/api/chat", {"text": "again"})
    assert status == 429
    assert int(headers["Retry-After"]) >= 1
    assert "too many requests" in data["error"]

