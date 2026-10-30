"""The Resend integration: signatures, storage, ingestion and the route.

The network is never touched. A real webhook is an HTTP POST with a Svix
signature; the tests sign their own payloads the same way, and the two API
calls (`get_received_email`, attachment download) are replaced by a fake
that returns a fixed message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest

from agent.mail import inbound
from agent.mail.resend import WebhookVerificationError, verify_webhook
from agent.mail.store import MailStore
from agent.memory.hermes_memory import HouseholdMemory
from interface.web.server import InkWellHandler

SECRET = "whsec_" + base64.b64encode(b"test-secret-key").decode("ascii")


def _sign(
    body: bytes,
    *,
    secret: str = SECRET,
    svix_id: str = "msg_1",
    timestamp: int | None = None,
) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{svix_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": svix_id,
        "svix-timestamp": stamp,
        "svix-signature": "v1," + base64.b64encode(digest).decode("ascii"),
    }


def _store(tmp_path) -> tuple[HouseholdMemory, MailStore]:
    memory = HouseholdMemory(tmp_path / "memory.db")
    memory.connect()
    return memory, MailStore(memory.connect())


# --- signature --------------------------------------------------------------


def test_a_valid_signature_is_accepted():
    body = b'{"type": "email.delivered"}'
    assert verify_webhook(body, _sign(body), SECRET) == {"type": "email.delivered"}


def test_a_changed_body_is_rejected():
    body = b'{"type": "email.delivered"}'
    with pytest.raises(WebhookVerificationError):
        verify_webhook(b'{"type": "email.bounced"}', _sign(body), SECRET)


def test_a_stale_timestamp_is_rejected():
    body = b"{}"
    headers = _sign(body, timestamp=int(time.time()) - 3600)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body, headers, SECRET)


def test_missing_headers_are_rejected():
    with pytest.raises(WebhookVerificationError):
        verify_webhook(b"{}", {}, SECRET)


def test_a_wrong_secret_is_rejected():
    body = b"{}"
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body, _sign(body), "whsec_" + base64.b64encode(b"other").decode())


# --- storage ----------------------------------------------------------------


def test_messages_and_events_are_idempotent(tmp_path):
    _, store = _store(tmp_path)
    assert store.record_message(message_id="mail_1", subject="Renewal") is True
    assert store.record_message(message_id="mail_1", subject="Renewal") is False

    assert store.add_event(event="email.received", message_id="mail_1", provider_event_id="evt_1")
    assert not store.add_event(
        event="email.received", message_id="mail_1", provider_event_id="evt_1"
    )
    assert store.summary() == {"messages": 1, "events": 1, "by_state": {"received": 1}}


# --- ingestion --------------------------------------------------------------


class FakeClient:
    configured = True

    def __init__(self, *, attachments: list[dict] | None = None) -> None:
        self.attachments = attachments or []
        self.fetched: list[str] = []

    def get_received_email(self, email_id: str) -> dict:
        self.fetched.append(email_id)
        return {
            "id": email_id,
            "from": "Northwind Home Insurance <notices@northwind.example>",
            "to": ["avery@example.com"],
            "subject": "Your renewal notice",
            "created_at": "2026-10-30T09:00:00.000Z",
            "message_id": "<notice-1@northwind.example>",
            "text": (
                "Northwind Home Insurance\n"
                "Renewal Notice\n\n"
                "Policy holder: Avery Quinn\n"
                "Policy number: NW-4471-8820\n"
                "Your policy renews on 15 December 2026.\n"
                "Annual premium: $1,284.00\n"
            ),
            "attachments": [
                {"id": row["id"], "filename": row["filename"]} for row in self.attachments
            ],
        }

    def list_attachments(self, email_id: str) -> list[dict]:
        return self.attachments

    def download(self, url: str) -> bytes:
        return b"Gym membership renews on 2027-01-05. Member number: 4455-1200.\n"


def _received_event(email_id: str = "mail_1") -> dict:
    return {
        "type": "email.received",
        "created_at": "2026-10-30T09:00:01.000Z",
        "data": {
            "email_id": email_id,
            "from": "notices@northwind.example",
            "to": ["avery@example.com"],
            "subject": "Your renewal notice",
            "message_id": "<notice-1@northwind.example>",
            "created_at": "2026-10-30T09:00:00.000Z",
            "attachments": [],
        },
    }


def test_an_email_becomes_a_tracked_item(tmp_path):
    memory, store = _store(tmp_path)
    result = inbound.handle_event(
        _received_event(), store=store, client=FakeClient(), memory=memory, provider_event_id="evt_1"
    )

    assert result["status"] == "processed"
    assert result["message_id"] == "mail_1"
    assert result["items"], "the body should yield an item"

    message = store.get_message("mail_1")
    assert message is not None
    assert message["item_id"] == result["items"][0]
    assert message["body_redacted"] is True
    assert "4471" not in (message["body"] or ""), "account numbers must not be stored"
    assert "Avery Quinn" not in (message["body"] or "")

    item = memory.get(result["items"][0])
    assert item is not None
    assert "Northwind" in item.provider


def test_a_second_delivery_of_the_same_email_is_a_duplicate(tmp_path):
    memory, store = _store(tmp_path)
    client = FakeClient()
    inbound.handle_event(
        _received_event(), store=store, client=client, memory=memory, provider_event_id="evt_1"
    )
    again = inbound.handle_event(
        _received_event(), store=store, client=client, memory=memory, provider_event_id="evt_2"
    )

    assert again["status"] == "duplicate"
    assert client.fetched == ["mail_1"], "the body must be fetched once"
    assert store.summary()["messages"] == 1
    assert store.summary()["events"] == 2


def test_an_attachment_is_recorded_and_read(tmp_path):
    memory, store = _store(tmp_path)
    attachment = {
        "id": "att_1",
        "filename": "gym.txt",
        "content_type": "text/plain",
        "download_url": "https://inbound-cdn.resend.com/att_1",
    }
    result = inbound.handle_event(
        _received_event(),
        store=store,
        client=FakeClient(attachments=[attachment]),
        memory=memory,
        provider_event_id="evt_1",
    )

    assert result["status"] == "processed"
    rows = store.attachments_for("mail_1")
    assert len(rows) == 1
    assert rows[0]["state"] in {"processed", "read"}
    assert rows[0]["filename"] == "gym.txt"


def test_a_message_without_a_key_is_stored_pending(tmp_path):
    _, store = _store(tmp_path)

    class NoKey:
        configured = False

    result = inbound.handle_event(
        _received_event(), store=store, client=NoKey(), memory=None, provider_event_id="evt_1"
    )

    assert result["status"] == "stored"
    assert store.get_message("mail_1")["state"] == "pending"


def test_delivery_events_update_the_message(tmp_path):
    _, store = _store(tmp_path)
    store.record_message(message_id="mail_1", direction="outbound", state="sent")

    result = inbound.handle_event(
        {
            "type": "email.delivered",
            "created_at": "2026-10-30T09:05:00.000Z",
            "data": {"email_id": "mail_1"},
        },
        store=store,
        client=None,
        memory=None,
        provider_event_id="evt_delivered",
    )

    assert result["status"] == "recorded"
    assert store.get_message("mail_1")["state"] == "delivered"
    assert store.events_for("mail_1")[0]["event"] == "email.delivered"


# --- the HTTP route ---------------------------------------------------------


@pytest.fixture
def web(tmp_path):
    httpd = HTTPServer(("127.0.0.1", 0), InkWellHandler)
    httpd.daemon_threads = True
    ready = threading.Event()

    def run() -> None:
        memory = HouseholdMemory(tmp_path / "memory.db")
        memory.connect()
        httpd.memory = memory  # type: ignore[attr-defined]
        httpd.demo_mode = True  # type: ignore[attr-defined]
        httpd.server_port = httpd.server_address[1]  # type: ignore[attr-defined]
        ready.set()
        try:
            httpd.serve_forever()
        finally:
            memory.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    yield httpd
    httpd.shutdown()
    thread.join(5)


def _post(port: int, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", "/api/resend/webhook", body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else {}
    finally:
        connection.close()


def _get(port: int, path: str) -> tuple[int, bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_mail_reads_are_available_on_the_local_server(web):
    status, body = _get(web.server_address[1], "/api/mail")
    assert status == 200
    assert json.loads(body)["messages"] == []


def test_mail_reads_are_hidden_on_the_public_demo(web, monkeypatch):
    monkeypatch.delenv("INKWELL_MAIL_PUBLIC", raising=False)
    web.public_demo = True  # type: ignore[attr-defined]

    for path in ("/api/mail", "/api/mail/mail_1"):
        status, _body = _get(web.server_address[1], path)
        assert status == 404, path


def test_mail_reads_can_be_opted_in_on_the_public_demo(web, monkeypatch):
    monkeypatch.setenv("INKWELL_MAIL_PUBLIC", "1")
    web.public_demo = True  # type: ignore[attr-defined]

    status, body = _get(web.server_address[1], "/api/mail")
    assert status == 200
    assert json.loads(body)["messages"] == []


def test_the_public_demo_webhook_is_disabled(web, monkeypatch):
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("INKWELL_MAIL_PUBLIC", raising=False)
    web.public_demo = True  # type: ignore[attr-defined]

    body = b'{"type": "email.delivered", "data": {"email_id": "mail_1"}}'
    status, payload = _post(web.server_address[1], body, _sign(body))
    assert status == 503
    assert "public demo" in payload["error"]


def test_the_route_refuses_to_run_unconfigured(web, monkeypatch):
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
    status, payload = _post(web.server_address[1], b"{}", {})
    assert status == 503
    assert "not configured" in payload["error"]


def test_the_route_rejects_a_bad_signature(web, monkeypatch):
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    status, payload = _post(
        web.server_address[1],
        b'{"type": "email.delivered", "data": {"email_id": "mail_1"}}',
        {"svix-id": "x", "svix-timestamp": str(int(time.time())), "svix-signature": "v1,bad"},
    )
    assert status == 400
    assert "signature" in payload["error"]


def test_the_route_records_a_signed_event(web, monkeypatch):
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    body = b'{"type": "email.delivered", "created_at": "2026-10-30T09:05:00Z", "data": {"email_id": "mail_9"}}'
    status, payload = _post(web.server_address[1], body, _sign(body, svix_id="evt_9"))

    assert status == 200
    assert payload["status"] == "recorded"

    connection = sqlite3.connect(web.memory.path)  # type: ignore[attr-defined]
    try:
        store = MailStore(connection)
        assert store.events_for("mail_9")[0]["event"] == "email.delivered"
    finally:
        connection.close()
