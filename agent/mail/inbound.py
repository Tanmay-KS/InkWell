"""Turn Resend webhooks into tracked household mail.

Two kinds of event arrive on the same endpoint:

* `email.received` — an inbound message. Resend sends metadata, so the
  handler fetches the body and attachments, redacts the body, stores it,
  and runs the deterministic graph over the text and each attachment.
* delivery events (`email.delivered`, `email.bounced`, ...) — recorded
  against the message they belong to, so the dashboard can show what
  happened after a draft was sent.

Nothing here sends mail. Outbound sending stays behind the existing
approval step, and the webhook path only ever reads.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.graph import run_document_graph, run_text_graph
from agent.ingestion.parse_email import _html_to_text, strip_quoted_chain
from agent.mail.resend import ResendClient, ResendError, verify_webhook
from agent.mail.store import MailStore
from agent.memory.hermes_memory import HouseholdMemory
from privacy_router.redact import redacted_text

MAX_ATTACHMENTS = max(0, int(os.environ.get("INKWELL_MAIL_MAX_ATTACHMENTS", "3")))
MAX_BODY_CHARS = 200_000

_SUFFIX_BY_TYPE = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
    "text/plain": ".txt",
    "message/rfc822": ".eml",
}


def _safe_suffix(filename: str | None, content_type: str | None) -> str:
    if filename:
        suffix = Path(Path(filename).name).suffix
        if suffix and len(suffix) <= 12:
            return suffix
    return _SUFFIX_BY_TYPE.get((content_type or "").lower(), ".bin")


def _redact_body(body: str, memory: HouseholdMemory | None) -> str:
    """Redact before the body is written down.

    A redaction failure must not fall back to storing the raw text. The
    message is still recorded, with an empty body, and the warning says so.
    """
    names = memory.household_names() if memory is not None else []
    try:
        redacted, _map = redacted_text(body, names=names)
    except Exception:  # the privacy boundary must not fail open
        return ""
    return redacted


def _process_attachments(
    *,
    email_id: str,
    received: dict[str, Any],
    client: ResendClient,
    store: MailStore,
    memory: HouseholdMemory | None,
    warnings: list[str],
) -> list[str]:
    if memory is None or MAX_ATTACHMENTS == 0:
        return []

    metadata = received.get("attachments") or []
    if not isinstance(metadata, list) or not metadata:
        return []

    try:
        listed = client.list_attachments(email_id)
    except ResendError as exc:
        warnings.append(f"attachments could not be listed: {exc}")
        return []

    item_ids: list[str] = []
    for attachment in listed[:MAX_ATTACHMENTS]:
        attachment_id = str(attachment.get("id") or "")
        filename = attachment.get("filename")
        content_type = attachment.get("content_type")
        url = attachment.get("download_url")
        if not attachment_id or not url:
            warnings.append("attachment metadata was incomplete")
            continue
        try:
            payload = client.download(str(url))
            suffix = _safe_suffix(filename if isinstance(filename, str) else None,
                                  content_type if isinstance(content_type, str) else None)
            with tempfile.NamedTemporaryFile(
                prefix="inkwell-mail-", suffix=suffix, delete=False
            ) as handle:
                handle.write(payload)
                path = Path(handle.name)
            try:
                state = run_document_graph(path, memory=memory)
            finally:
                path.unlink(missing_ok=True)
            item_id = state.item.item_id if state.item is not None else None
            if item_id:
                item_ids.append(item_id)
            store.record_attachment(
                attachment_id=attachment_id,
                message_id=email_id,
                filename=filename if isinstance(filename, str) else None,
                content_type=content_type if isinstance(content_type, str) else None,
                size=len(payload),
                item_id=item_id,
                state="processed" if item_id else "read",
            )
        except (ResendError, OSError, ValueError) as exc:
            warnings.append(f"attachment {filename or attachment_id}: {type(exc).__name__}")
            store.record_attachment(
                attachment_id=attachment_id,
                message_id=email_id,
                filename=filename if isinstance(filename, str) else None,
                content_type=content_type if isinstance(content_type, str) else None,
                state="error",
                detail=type(exc).__name__,
            )
    return item_ids


def _item_from_text(body: str, memory: HouseholdMemory | None, warnings: list[str]) -> str | None:
    if not body or memory is None:
        return None
    try:
        state = run_text_graph(body, memory=memory)
    except Exception as exc:
        warnings.append(f"email body could not be processed ({type(exc).__name__})")
        return None
    return state.item.item_id if state.item is not None else None


def _receive(
    data: dict[str, Any],
    *,
    store: MailStore,
    client: ResendClient | None,
    memory: HouseholdMemory | None,
    provider_event_id: str | None,
) -> dict[str, Any]:
    email_id = str(data.get("email_id") or "")
    if not email_id:
        return {"status": "ignored", "warning": "event carried no email_id"}

    if store.exists(email_id):
        store.add_event(
            provider_event_id=provider_event_id,
            message_id=email_id,
            event="email.received",
            occurred_at=data.get("created_at"),
        )
        return {"status": "duplicate", "message_id": email_id}

    metadata = {
        "authentication": data.get("authentication"),
        "received_for": data.get("received_for") or [],
        "reply_to": data.get("reply_to") or [],
        "cc": data.get("cc") or [],
        "bcc": data.get("bcc") or [],
    }
    recipients = data.get("to") if isinstance(data.get("to"), list) else []

    if client is None or not client.configured:
        store.record_message(
            message_id=email_id,
            internet_message_id=data.get("message_id"),
            sender=str(data.get("from") or "") or None,
            recipients=[str(value) for value in recipients],
            subject=str(data.get("subject") or "") or None,
            received_at=data.get("created_at"),
            state="pending",
            metadata=metadata,
        )
        store.add_event(
            provider_event_id=provider_event_id,
            message_id=email_id,
            event="email.received",
            occurred_at=data.get("created_at"),
        )
        return {
            "status": "stored",
            "message_id": email_id,
            "warnings": ["RESEND_API_KEY is not set; the body was not fetched"],
        }

    received = client.get_received_email(email_id)
    body = received.get("text") or ""
    if not body and received.get("html"):
        body = _html_to_text(str(received["html"]))
    body = strip_quoted_chain(str(body))[:MAX_BODY_CHARS]

    warnings: list[str] = []
    stored_body = _redact_body(body, memory) if body else ""
    if body and not stored_body:
        warnings.append("the body could not be redacted and was not stored")

    created = store.record_message(
        message_id=email_id,
        internet_message_id=received.get("message_id") or data.get("message_id"),
        sender=str(received.get("from") or data.get("from") or "") or None,
        recipients=[
            str(value)
            for value in (received.get("to") or recipients)
            if isinstance(value, str)
        ],
        subject=str(received.get("subject") or data.get("subject") or "") or None,
        body=stored_body,
        body_redacted=True,
        received_at=received.get("created_at") or data.get("created_at"),
        state="stored",
        metadata=metadata,
    )
    if not created:
        return {"status": "duplicate", "message_id": email_id}

    item_ids = [item for item in [_item_from_text(body, memory, warnings)] if item]
    item_ids.extend(
        _process_attachments(
            email_id=email_id,
            received=received,
            client=client,
            store=store,
            memory=memory,
            warnings=warnings,
        )
    )

    # Keep the first item as the message's headline link; the rest still
    # exist in the household table and point back through their source.
    unique_items = list(dict.fromkeys(item_ids))
    store.update_message(
        email_id,
        state="processed" if unique_items else "stored",
        item_id=unique_items[0] if unique_items else None,
    )
    store.add_event(
        provider_event_id=provider_event_id,
        message_id=email_id,
        event="email.received",
        occurred_at=data.get("created_at"),
    )
    return {
        "status": "processed" if unique_items else "stored",
        "message_id": email_id,
        "items": unique_items,
        "warnings": warnings,
    }


def handle_event(
    event: dict[str, Any],
    *,
    store: MailStore,
    client: ResendClient | None,
    memory: HouseholdMemory | None,
    provider_event_id: str | None = None,
) -> dict[str, Any]:
    """Dispatch one already-verified webhook event."""
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    assert isinstance(data, dict)

    if event_type == "email.received":
        return _receive(
            data,
            store=store,
            client=client,
            memory=memory,
            provider_event_id=provider_event_id,
        )

    email_id = str(data.get("email_id") or "")
    store.add_event(
        provider_event_id=provider_event_id,
        message_id=email_id or None,
        event=event_type or "unknown",
        detail=str(data.get("reason") or "") or None,
        occurred_at=event.get("created_at"),
    )
    state = {
        "email.delivered": "delivered",
        "email.bounced": "bounced",
        "email.complained": "complained",
    }.get(event_type)
    if state and email_id and store.exists(email_id):
        store.update_message(email_id, state=state)
    return {"status": "recorded", "event": event_type, "message_id": email_id or None}


def handle_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    store: MailStore,
    client: ResendClient | None,
    memory: HouseholdMemory | None,
    secret: str,
) -> dict[str, Any]:
    """Verify and dispatch one webhook request."""
    event = verify_webhook(raw_body, headers, secret)
    provider_event_id = None
    for key, value in headers.items():
        if key.lower() == "svix-id":
            provider_event_id = str(value)
            break
    return handle_event(
        event,
        store=store,
        client=client,
        memory=memory,
        provider_event_id=provider_event_id,
    )
