"""Read a real mailbox over IMAP, without taking it over.

The design constraint is that importing mail must not change the mailbox.
So the connection is opened `readonly=True`, messages are fetched with
`BODY.PEEK[]` (which does not set the Seen flag), and progress is tracked
with a stored watermark instead of server-side flags.

A watermark is `(UIDVALIDITY, last UID)`. When the server reports a new
`UIDVALIDITY` the mailbox was recreated and unrelated UIDs must not be
skipped, so the watermark is discarded and the import starts again from
the most recent window.

Only the newest few messages are imported on the first pass. A mailbox
with ten years of mail is not a migration target; the product needs the
notices that arrived recently. `--limit` raises the window deliberately.

Parsing is the standard library's MIME parser. The body is redacted
before it is stored, exactly as the Resend path does, and attachments go
through the same document graph.
"""

from __future__ import annotations

import contextlib
import imaplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from agent.ingestion.parse_email import _html_to_text, strip_quoted_chain
from agent.mail.config import ImapSettings
from agent.mail.inbound import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_BODY_CHARS,
    item_from_text,
    process_attachment_bytes,
    redact_body,
)
from agent.mail.store import MailStore
from agent.memory.hermes_memory import HouseholdMemory

__all__ = [
    "ImapClient",
    "ImapError",
    "ParsedAttachment",
    "ParsedMessage",
    "ingest_parsed",
    "parse_message",
]

DEFAULT_LIMIT = 20


class ImapError(RuntimeError):
    """An IMAP conversation failed."""


@dataclass(slots=True)
class ParsedAttachment:
    filename: str
    content_type: str
    payload: bytes


@dataclass(slots=True)
class ParsedMessage:
    message_id: str
    uid: int
    uidvalidity: int | None
    sender: str
    recipients: list[str]
    subject: str
    body: str
    received_at: str | None
    attachments: list[ParsedAttachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, (bytes, bytearray)):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return bytes(payload).decode(charset, errors="replace")
    except LookupError:
        return bytes(payload).decode("utf-8", errors="replace")


def _body_text(message: Message) -> str:
    plain = ""
    html = ""
    for part in message.walk():
        if part.is_multipart() or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and not plain:
            plain = _decode_part(part)
        elif content_type == "text/html" and not html:
            html = _decode_part(part)
    if plain.strip():
        return strip_quoted_chain(plain)[:MAX_BODY_CHARS]
    if html.strip():
        return strip_quoted_chain(_html_to_text(html))[:MAX_BODY_CHARS]
    return ""


def _received_at(message: Message) -> str | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _attachments(message: Message, warnings: list[str]) -> list[ParsedAttachment]:
    if MAX_ATTACHMENTS <= 0:
        return []
    found: list[ParsedAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        if len(found) >= MAX_ATTACHMENTS:
            warnings.append(f"attachment {filename}: beyond the first {MAX_ATTACHMENTS}")
            break
        payload = part.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            continue
        if len(payload) > MAX_ATTACHMENT_BYTES:
            warnings.append(f"attachment {filename}: over the 15 MiB limit")
            continue
        found.append(
            ParsedAttachment(
                filename=str(filename),
                content_type=part.get_content_type() or "application/octet-stream",
                payload=bytes(payload),
            )
        )
    return found


def parse_message(raw: bytes, *, uid: int, uidvalidity: int | None) -> ParsedMessage:
    """Turn one RFC 822 message into the fields the store needs."""
    message = message_from_bytes(raw, policy=default_policy)
    warnings: list[str] = []
    message_id = str(message.get("Message-ID") or "").strip()
    if not message_id:
        message_id = f"imap:{uidvalidity or 0}:{uid}"
    to_header = str(message.get("To") or "")
    recipients = [address for _name, address in getaddresses([to_header]) if address]
    return ParsedMessage(
        message_id=message_id,
        uid=uid,
        uidvalidity=uidvalidity,
        sender=str(message.get("From") or "").strip(),
        recipients=recipients,
        subject=str(message.get("Subject") or "").strip(),
        body=_body_text(message),
        received_at=_received_at(message),
        attachments=_attachments(message, warnings),
        warnings=warnings,
    )


def ingest_parsed(
    parsed: ParsedMessage,
    *,
    store: MailStore,
    memory: HouseholdMemory | None,
    provider_event_id: str | None,
    account: str,
) -> dict[str, Any]:
    """Store one parsed message and run the deterministic graph over it."""
    if store.exists(parsed.message_id):
        store.add_event(
            provider_event_id=provider_event_id,
            message_id=parsed.message_id,
            event="email.received",
            occurred_at=parsed.received_at,
        )
        return {
            "status": "duplicate",
            "message_id": parsed.message_id,
            "uid": parsed.uid,
        }

    warnings = list(parsed.warnings)
    stored_body = redact_body(parsed.body, memory) if parsed.body else ""
    if parsed.body and not stored_body:
        warnings.append("the body could not be redacted and was not stored")

    created = store.record_message(
        message_id=parsed.message_id,
        direction="inbound",
        internet_message_id=parsed.message_id,
        sender=parsed.sender or None,
        recipients=parsed.recipients,
        subject=parsed.subject or None,
        body=stored_body,
        body_redacted=True,
        received_at=parsed.received_at,
        state="stored",
        metadata={
            "transport": "imap",
            "account": account,
            "uid": parsed.uid,
            "uidvalidity": parsed.uidvalidity,
        },
    )
    if not created:
        return {"status": "duplicate", "message_id": parsed.message_id, "uid": parsed.uid}

    item_ids = [item for item in [item_from_text(parsed.body, memory, warnings)] if item]
    for index, attachment in enumerate(parsed.attachments):
        item_id = process_attachment_bytes(
            attachment_id=f"{account}:{parsed.uidvalidity}:{parsed.uid}:{index}",
            message_id=parsed.message_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            payload=attachment.payload,
            store=store,
            memory=memory,
            warnings=warnings,
        )
        if item_id:
            item_ids.append(item_id)

    unique_items = list(dict.fromkeys(item_ids))
    store.update_message(
        parsed.message_id,
        state="processed" if unique_items else "stored",
        item_id=unique_items[0] if unique_items else None,
    )
    store.add_event(
        provider_event_id=provider_event_id,
        message_id=parsed.message_id,
        event="email.received",
        occurred_at=parsed.received_at,
    )
    return {
        "status": "processed" if unique_items else "stored",
        "message_id": parsed.message_id,
        "uid": parsed.uid,
        "items": unique_items,
        "warnings": warnings,
    }


def _response_int(connection: Any, name: str) -> int | None:
    try:
        _status, data = connection.response(name)
    except (imaplib.IMAP4.error, ValueError, TypeError):
        return None
    if not data or data[0] is None:
        return None
    try:
        return int(data[0])
    except (TypeError, ValueError):
        return None


class ImapClient:
    """One mailbox, read-only, connected on demand."""

    def __init__(
        self,
        settings: ImapSettings | None = None,
        *,
        factory: Callable[[ImapSettings], Any] | None = None,
    ) -> None:
        self.settings = settings or ImapSettings.from_env()
        self._factory = factory

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def _connect(self) -> Any:
        self.settings.validate()
        if self._factory is not None:
            connection = self._factory(self.settings)
        else:
            try:
                if self.settings.ssl:
                    connection = imaplib.IMAP4_SSL(
                        self.settings.host,
                        self.settings.port,
                        timeout=self.settings.timeout,
                    )
                else:
                    connection = imaplib.IMAP4(
                        self.settings.host,
                        self.settings.port,
                        timeout=self.settings.timeout,
                    )
                    if self.settings.starttls:
                        connection.starttls(ssl_context=ssl.create_default_context())
            except (OSError, imaplib.IMAP4.error) as exc:
                raise ImapError(
                    f"could not connect to {self.settings.host}:{self.settings.port} "
                    f"({type(exc).__name__})"
                ) from None

        try:
            connection.login(self.settings.user, self.settings.password)
        except Exception as exc:
            self._close(connection)
            raise ImapError(
                f"IMAP login failed for {self.settings.user}@{self.settings.host} "
                f"({type(exc).__name__})"
            ) from None
        return connection

    @staticmethod
    def _close(connection: Any) -> None:
        try:
            connection.logout()
        except Exception:
            with contextlib.suppress(Exception):
                connection.shutdown()

    def _select(self, connection: Any, mailbox: str) -> dict[str, Any]:
        try:
            status, data = connection.select(mailbox, readonly=True)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"could not open {mailbox} ({type(exc).__name__})") from None
        if status != "OK":
            raise ImapError(f"could not open {mailbox} (server said {status})")
        try:
            messages = int(data[0]) if data and data[0] is not None else 0
        except (TypeError, ValueError):
            messages = 0
        return {
            "mailbox": mailbox,
            "messages": messages,
            "uidvalidity": _response_int(connection, "UIDVALIDITY"),
            "uidnext": _response_int(connection, "UIDNEXT"),
        }

    @staticmethod
    def _search(connection: Any, start: int, limit: int) -> list[int]:
        try:
            status, data = connection.uid("SEARCH", None, "UID", f"{start}:*")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"could not search the mailbox ({type(exc).__name__})") from None
        if status != "OK":
            raise ImapError(f"could not search the mailbox (server said {status})")
        raw = data[0] if data else b""
        tokens = raw.split() if isinstance(raw, (bytes, bytearray)) else str(raw).split()
        uids: list[int] = []
        for token in tokens:
            try:
                uid = int(token)
            except (TypeError, ValueError):
                continue
            # `UID n:*` returns the highest UID even when n is past the end.
            if uid >= start:
                uids.append(uid)
        return sorted(set(uids))[:limit]

    @staticmethod
    def _fetch(connection: Any, uid: int) -> bytes:
        try:
            status, data = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"could not fetch UID {uid} ({type(exc).__name__})") from None
        if status != "OK":
            raise ImapError(f"could not fetch UID {uid} (server said {status})")
        for item in data or []:
            if (
                isinstance(item, tuple)
                and len(item) >= 2
                and isinstance(item[1], (bytes, bytearray))
            ):
                return bytes(item[1])
        raise ImapError(f"UID {uid} carried no body")

    def verify(self) -> dict[str, Any]:
        """Connect, open the mailbox read-only, and report its size."""
        connection = self._connect()
        try:
            return self._select(connection, self.settings.mailbox)
        finally:
            self._close(connection)

    def sync(
        self,
        *,
        store: MailStore,
        memory: HouseholdMemory | None,
        limit: int = DEFAULT_LIMIT,
        mailbox: str | None = None,
    ) -> dict[str, Any]:
        """Import messages newer than the stored watermark."""
        target = mailbox or self.settings.mailbox
        limit = max(1, min(int(limit), 200))
        connection = self._connect()
        try:
            selected = self._select(connection, target)
            uidvalidity = selected["uidvalidity"]
            state = store.get_sync_state(self.settings.account, target)
            last_uid = 0
            if state and (
                state.get("uidvalidity") is None
                or uidvalidity is None
                or state.get("uidvalidity") == uidvalidity
            ):
                last_uid = int(state.get("last_uid") or 0)

            uidnext = selected["uidnext"]
            start = last_uid + 1 if last_uid else max(1, (uidnext or limit + 1) - limit)
            uids = self._search(connection, start, limit)

            results: list[dict[str, Any]] = []
            for uid in uids:
                try:
                    raw = self._fetch(connection, uid)
                    parsed = parse_message(raw, uid=uid, uidvalidity=uidvalidity)
                    result = ingest_parsed(
                        parsed,
                        store=store,
                        memory=memory,
                        provider_event_id=f"imap:{uidvalidity}:{uid}",
                        account=self.settings.account,
                    )
                except ImapError as exc:
                    result = {"status": "error", "uid": uid, "warning": str(exc)}
                store.set_sync_state(
                    self.settings.account,
                    target,
                    uidvalidity=uidvalidity,
                    last_uid=uid,
                )
                results.append(result)

            return {
                "mailbox": target,
                "uidvalidity": uidvalidity,
                "checked": len(uids),
                "last_uid": uids[-1] if uids else last_uid,
                "results": results,
            }
        finally:
            self._close(connection)
