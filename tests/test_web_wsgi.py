"""Tests for the WSGI front end used by the public demo deployment.

The deployment must behave like `make demo-web` while never touching the
household sidecar. These tests call the WSGI callable directly, so they
need no socket and no Vercel.
"""

from __future__ import annotations

import io
import json

import pytest

import interface.web.wsgi as wsgi


@pytest.fixture(autouse=True)
def public_demo(monkeypatch):
    """Every test runs as the public demo unless it says otherwise."""
    monkeypatch.setattr(wsgi, "PUBLIC_DEMO", True)


def _call(method: str, path: str, *, body: bytes = b"", headers: dict | None = None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)),
        "REMOTE_ADDR": "203.0.113.9",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTP_HOST": "inkwell.vercel.app",
    }
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value

    captured: dict = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    payload = b"".join(wsgi.app(environ, start_response))
    return captured["status"], captured["headers"], payload


def test_the_demo_serves_the_real_page():
    status, headers, body = _call("GET", "/")

    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/html")
    assert b"<!doctype html>" in body


def test_the_demo_is_marked_as_fictional():
    status, _, body = _call("GET", "/api/summary")
    data = json.loads(body)

    assert status == "200 OK"
    assert data["demo"] is True
    assert len(data["items"]) >= 5
    assert data["findings"]


def test_the_public_host_is_allowed_only_in_demo_mode(monkeypatch):
    monkeypatch.setattr(wsgi, "PUBLIC_DEMO", False)
    status, _, _ = _call("GET", "/api/summary")
    assert status == "403 Forbidden"

    monkeypatch.setattr(wsgi, "PUBLIC_DEMO", True)
    status, _, _ = _call("GET", "/api/summary")
    assert status == "200 OK"


def test_chat_answers_against_the_demo_database():
    status, _, body = _call(
        "POST",
        "/api/chat",
        body=json.dumps({"text": "When does Orchard Fitness renew?"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    reply = json.loads(body)["reply"]

    assert status == "200 OK"
    assert "Orchard Fitness" in reply


def test_an_unknown_path_is_a_404_with_a_length():
    status, headers, body = _call("GET", "/nope")

    assert status == "404 Not Found"
    assert headers["Content-Length"] == str(len(body))
