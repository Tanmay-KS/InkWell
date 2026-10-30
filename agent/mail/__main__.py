"""Command line helpers for the Resend integration.

    python -m agent.mail doctor
    python -m agent.mail list
    python -m agent.mail webhook-create --endpoint https://example.com/api/resend/webhook
    python -m agent.mail send --to you@example.com --subject "Hello" --text "..."

`doctor` is the one to run first: it distinguishes a send-only key from a
full-access one, which is the failure mode that otherwise looks like an
empty inbox.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agent.mail import inbound
from agent.mail.resend import ResendClient, ResendError
from agent.mail.store import MailStore
from agent.memory.hermes_memory import HouseholdMemory

RECEIVING_EVENTS = [
    "email.received",
    "email.sent",
    "email.delivered",
    "email.delivery_delayed",
    "email.bounced",
    "email.complained",
    "email.opened",
    "email.clicked",
]


def _store() -> tuple[HouseholdMemory, MailStore]:
    memory = HouseholdMemory()
    memory.connect()
    return memory, MailStore(memory.connect())


def _mask(key: str) -> str:
    if not key:
        return "missing"
    return f"{key[:6]}…{key[-4:]} ({len(key)} chars)"


def _doctor() -> int:
    client = ResendClient()
    print(f"RESEND_API_KEY: {_mask(client.api_key)}")
    print(f"RESEND_WEBHOOK_SECRET: {'set' if _secret() else 'missing'}")
    if not client.configured:
        print("Set RESEND_API_KEY in .env before receiving mail.")
        return 1

    failures = 0
    for label, call in (
        ("domains", client.list_domains),
        ("webhooks", client.list_webhooks),
        ("received", lambda: client.list_received_emails(5)),
    ):
        try:
            rows = call()
        except ResendError as exc:
            print(f"{label}: FAILED — {exc}")
            if "restricted" in str(exc):
                print("  The key can send but not read. Use a full-access key for inbound.")
            failures += 1
            continue
        print(f"{label}: {len(rows)}")

    _, store = _store()
    summary = store.summary()
    print(f"local mail rows: {summary['messages']} messages, {summary['events']} events")
    print(f"states: {json.dumps(summary['by_state'], sort_keys=True)}")
    return 1 if failures else 0


def _secret() -> str:
    import os

    return os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()


def _sync(limit: int) -> int:
    """Import received mail that arrived while no webhook was listening.

    This is the path that makes the local product work without exposing a
    public endpoint: the household machine asks Resend what arrived, and
    the body is fetched and redacted exactly as the webhook does.
    """
    client = ResendClient()
    if not client.configured:
        print("RESEND_API_KEY is not set", file=sys.stderr)
        return 1
    try:
        rows = client.list_received_emails(limit)
    except ResendError as exc:
        print(f"could not list received email: {exc}", file=sys.stderr)
        return 1

    memory, store = _store()
    imported = 0
    for row in reversed(rows):
        email_id = str(row.get("id") or row.get("email_id") or "")
        if not email_id or store.exists(email_id):
            continue
        event = {
            "type": "email.received",
            "created_at": row.get("created_at"),
            "data": {**row, "email_id": email_id},
        }
        result = inbound.handle_event(
            event,
            store=store,
            client=client,
            memory=memory,
            provider_event_id=f"sync:{email_id}",
        )
        items = result.get("items") or []
        print(f"{email_id}  {result['status']}  items={len(items)}")
        imported += 1
    print(f"{imported} new message(s) from {len(rows)} checked")
    return 0


def _list(limit: int) -> int:
    _, store = _store()
    rows = store.list_messages(limit)
    if not rows:
        print("no mail yet")
        return 0
    for row in rows:
        when = row.get("received_at") or row.get("created_at") or ""
        sender = row.get("sender") or "?"
        subject = row.get("subject") or "(no subject)"
        print(f"{when[:19]:19}  {row['state']:10}  {sender[:38]:38}  {subject[:60]}")
    return 0


def _show(message_id: str) -> int:
    _, store = _store()
    message = store.get_message(message_id)
    if message is None:
        print(f"no message {message_id}", file=sys.stderr)
        return 1
    payload: dict[str, Any] = dict(message)
    payload["events"] = store.events_for(message_id)
    payload["attachments"] = store.attachments_for(message_id)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _webhook_create(endpoint: str, events: list[str]) -> int:
    client = ResendClient()
    try:
        created = client.create_webhook(endpoint, events)
    except ResendError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(created, indent=2))
    print(
        "\nPut signing_secret in RESEND_WEBHOOK_SECRET (local .env and the "
        "deployment's environment). Do not commit it.",
        file=sys.stderr,
    )
    return 0


def _send(sender: str, to: str, subject: str, text: str) -> int:
    client = ResendClient()
    try:
        sent = client.send(sender=sender, to=[to], subject=subject, text=text)
    except ResendError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(sent, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.mail", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the key, endpoints and local store")

    list_parser = sub.add_parser("list", help="list stored messages")
    list_parser.add_argument("--limit", type=int, default=20)

    sync_parser = sub.add_parser("sync", help="import new received mail from Resend")
    sync_parser.add_argument("--limit", type=int, default=20)

    show_parser = sub.add_parser("show", help="show one message and its events")
    show_parser.add_argument("message_id")

    hook_parser = sub.add_parser("webhook-create", help="create the Resend webhook")
    hook_parser.add_argument("--endpoint", required=True)
    hook_parser.add_argument("--event", action="append", default=[])
    hook_parser.add_argument("--receiving-only", action="store_true")

    send_parser = sub.add_parser("send", help="send one email after approval")
    send_parser.add_argument("--from", dest="sender", default="InkWell <onboarding@resend.dev>")
    send_parser.add_argument("--to", required=True)
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--text", required=True)

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor()
    if args.command == "list":
        return _list(args.limit)
    if args.command == "sync":
        return _sync(args.limit)
    if args.command == "show":
        return _show(args.message_id)
    if args.command == "webhook-create":
        events = args.event or (["email.received"] if args.receiving_only else RECEIVING_EVENTS)
        return _webhook_create(args.endpoint, events)
    if args.command == "send":
        return _send(args.sender, args.to, args.subject, args.text)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
