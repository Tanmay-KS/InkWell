"""Command line helpers for the mail transports.

    python -m agent.mail doctor              # configuration, offline
    python -m agent.mail doctor --online     # connect and authenticate
    python -m agent.mail list
    python -m agent.mail imap-sync --limit 20
    python -m agent.mail sync                # Resend receiving
    python -m agent.mail send --to you@example.com --subject "Hello" --text "..."
    python -m agent.mail send --transport smtp ...

`send` is the only path that writes to the outside world, and it is meant
to be called after the user approves a draft. `imap-sync` and `sync` only
read.

`doctor` is the one to run first. It distinguishes a send-only Resend key
from a full-access one, reports whether IMAP/SMTP are configured, and with
`--online` actually logs in to each.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv

from agent.mail import inbound
from agent.mail.config import ImapSettings, SmtpSettings
from agent.mail.imap import ImapClient, ImapError
from agent.mail.inbound import redact_body
from agent.mail.resend import ResendClient, ResendError
from agent.mail.smtp import SmtpClient, SmtpError
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


def _secret() -> str:
    import os

    return os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()


def _doctor(online: bool) -> int:
    resend = ResendClient()
    imap = ImapSettings.from_env()
    smtp = SmtpSettings.from_env()

    print(f"RESEND_API_KEY: {_mask(resend.api_key)}")
    print(f"RESEND_WEBHOOK_SECRET: {'set' if _secret() else 'missing'}")
    if imap.configured:
        print(f"IMAP: {imap.user}@{imap.host}:{imap.port} ({imap.mailbox})")
    else:
        print("IMAP: not configured")
    if smtp.configured:
        print(f"SMTP: {smtp.user}@{smtp.host}:{smtp.port}")
    else:
        print("SMTP: not configured")

    failures = 0
    if online:
        if imap.configured:
            try:
                info = ImapClient(imap).verify()
                print(f"  imap: {info['messages']} message(s) in {info['mailbox']}")
            except (ImapError, ValueError) as exc:
                print(f"  imap: FAILED — {exc}")
                failures += 1
        if smtp.configured:
            try:
                SmtpClient(smtp).verify()
                print("  smtp: login ok")
            except (SmtpError, ValueError) as exc:
                print(f"  smtp: FAILED — {exc}")
                failures += 1
        if resend.configured:
            for label, call in (
                ("domains", resend.list_domains),
                ("webhooks", resend.list_webhooks),
                ("received", lambda: resend.list_received_emails(5)),
            ):
                try:
                    rows = call()
                except ResendError as exc:
                    print(f"  resend {label}: FAILED — {exc}")
                    if "restricted" in str(exc):
                        print("    The key can send but not read. Use a full-access key.")
                    failures += 1
                    continue
                print(f"  resend {label}: {len(rows)}")

    _, store = _store()
    summary = store.summary()
    print(f"local mail rows: {summary['messages']} messages, {summary['events']} events")
    print(f"states: {json.dumps(summary['by_state'], sort_keys=True)}")

    if not any((resend.configured, imap.configured, smtp.configured)):
        print("No transport is configured. Set RESEND_API_KEY or the SMTP/IMAP block.")
        return 1
    return 1 if failures else 0


def _sync(limit: int) -> int:
    """Import received mail that arrived while no webhook was listening."""
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


def _imap_sync(limit: int, mailbox: str | None) -> int:
    """Import from a real mailbox over IMAP, read-only."""
    settings = ImapSettings.from_env()
    if not settings.configured:
        print("IMAP is not configured: set INKWELL_IMAP_HOST/USER/PASSWORD", file=sys.stderr)
        return 1
    memory, store = _store()
    try:
        report = ImapClient(settings).sync(
            store=store,
            memory=memory,
            limit=limit,
            mailbox=mailbox,
        )
    except (ImapError, ValueError) as exc:
        print(f"IMAP sync failed: {exc}", file=sys.stderr)
        return 1

    for result in report["results"]:
        key = result.get("message_id") or f"uid {result.get('uid')}"
        items = result.get("items") or []
        extra = f"  {result['warning']}" if result.get("warning") else ""
        print(f"{key}  {result['status']}  items={len(items)}{extra}")
    print(
        f"{report['checked']} checked in {report['mailbox']}; "
        f"watermark uid {report['last_uid']}"
    )
    return 0


def _list(limit: int, direction: str | None) -> int:
    _, store = _store()
    rows = store.list_messages(limit, direction=direction)
    if not rows:
        print("no mail yet")
        return 0
    for row in rows:
        when = row.get("received_at") or row.get("created_at") or ""
        sender = row.get("sender") or "?"
        subject = row.get("subject") or "(no subject)"
        mark = "<-" if row.get("direction") == "inbound" else "->"
        print(
            f"{when[:19]:19}  {mark}  {row['state']:10}  "
            f"{sender[:36]:36}  {subject[:56]}"
        )
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


def _record_outbound(
    *,
    memory: HouseholdMemory,
    store: MailStore,
    message_id: str,
    sender: str,
    recipients: list[str],
    subject: str,
    body: str | None,
    transport: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Keep a redacted copy of what was sent, next to delivery events."""
    store.record_message(
        message_id=message_id,
        direction="outbound",
        internet_message_id=message_id,
        sender=sender,
        recipients=recipients,
        subject=subject,
        body=redact_body(body or "", memory),
        body_redacted=True,
        received_at=datetime.now(UTC).isoformat(timespec="seconds"),
        state="sent",
        metadata={"transport": transport, **(metadata or {})},
    )


def _send(
    transport: str,
    sender: str | None,
    to: str,
    subject: str,
    text: str | None,
    html: str | None,
) -> int:
    smtp_settings = SmtpSettings.from_env()
    use_smtp = transport == "smtp" or (transport == "auto" and smtp_settings.configured)

    memory, store = _store()
    if use_smtp:
        if not smtp_settings.configured:
            print("SMTP is not configured", file=sys.stderr)
            return 1
        client = SmtpClient(smtp_settings, sender=sender)
        try:
            sent = client.send(to=[to], subject=subject, text=text, html=html)
        except (SmtpError, ValueError) as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return 1
        _record_outbound(
            memory=memory,
            store=store,
            message_id=sent.message_id,
            sender=sent.sender,
            recipients=sent.recipients,
            subject=sent.subject,
            body=text or html,
            transport="smtp",
            metadata={"accepted": sent.accepted, "refused": sorted(sent.refused)},
        )
        print(json.dumps(sent.as_dict(), indent=2))
        return 0

    client = ResendClient()
    from_address = sender or "InkWell <onboarding@resend.dev>"
    try:
        sent = client.send(
            sender=from_address,
            to=[to],
            subject=subject,
            text=text,
            html=html,
        )
    except (ResendError, ValueError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    message_id = str(sent.get("id") or "")
    if message_id:
        _record_outbound(
            memory=memory,
            store=store,
            message_id=message_id,
            sender=from_address,
            recipients=[to],
            subject=subject,
            body=text or html,
            transport="resend",
        )
    print(json.dumps(sent, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.mail", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="check configuration and the local store")
    doctor_parser.add_argument(
        "--online",
        action="store_true",
        help="connect and authenticate to every configured transport",
    )

    list_parser = sub.add_parser("list", help="list stored messages")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--direction", choices=["inbound", "outbound"], default=None)

    sync_parser = sub.add_parser("sync", help="import new received mail from Resend")
    sync_parser.add_argument("--limit", type=int, default=20)

    imap_parser = sub.add_parser("imap-sync", help="import new mail from an IMAP mailbox")
    imap_parser.add_argument("--limit", type=int, default=20)
    imap_parser.add_argument("--mailbox", default=None)

    show_parser = sub.add_parser("show", help="show one message and its events")
    show_parser.add_argument("message_id")

    hook_parser = sub.add_parser("webhook-create", help="create the Resend webhook")
    hook_parser.add_argument("--endpoint", required=True)
    hook_parser.add_argument("--event", action="append", default=[])
    hook_parser.add_argument("--receiving-only", action="store_true")

    send_parser = sub.add_parser("send", help="send one email after approval")
    send_parser.add_argument(
        "--transport",
        choices=["auto", "resend", "smtp"],
        default="auto",
        help="auto uses SMTP when it is configured, Resend otherwise",
    )
    send_parser.add_argument("--from", dest="sender", default=None)
    send_parser.add_argument("--to", required=True)
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--text", default=None)
    send_parser.add_argument("--html", default=None)

    args = parser.parse_args(argv)

    # `.env` is the project's credential file. `load_dotenv` does not
    # override an explicitly exported variable, so a shell environment
    # still wins over the file.
    load_dotenv(override=False)

    if args.command == "doctor":
        return _doctor(args.online)
    if args.command == "list":
        return _list(args.limit, args.direction)
    if args.command == "sync":
        return _sync(args.limit)
    if args.command == "imap-sync":
        return _imap_sync(args.limit, args.mailbox)
    if args.command == "show":
        return _show(args.message_id)
    if args.command == "webhook-create":
        events = args.event or (["email.received"] if args.receiving_only else RECEIVING_EVENTS)
        return _webhook_create(args.endpoint, events)
    if args.command == "send":
        if not args.text and not args.html:
            print("provide --text or --html", file=sys.stderr)
            return 2
        return _send(args.transport, args.sender, args.to, args.subject, args.text, args.html)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
