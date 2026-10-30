"""Email parsing.

Phase 1 — Person B.

Emails are the other half of the ingestion story: policy renewals arrive by
email far more often than as documents. The parser is deliberately thinner
than the document parser because an email is already text — the work is
finding the attachment, stripping the quoted reply chain, and pulling the
first message out of the thread.

A note on the quoted chain: it is the single largest source of accidental
leakage in email ingestion, because a renewal notice's reply chain can carry
an account number, a support agent's name, and a phone number that the
original message never had. The chain is stripped before the text reaches
any model.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

# Subject prefixes that are not part of the subject.
_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|wg|aw|sv|vs|rif|tr)\s*(?:\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)

# Quoting styles, in the order they are stripped. Each pattern matches a
# line that starts a quoted or attributed block.
_QUOTE_MARKERS = [
    re.compile(r"^\s*>"),
    re.compile(r"^\s*On .+ wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*From:\s.+\s*$", re.IGNORECASE),
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),
]

# The signature separator, below which is the sender's own signature
# rather than the message.
_SIGNATURE_MARKER = re.compile(r"^\s*--\s*$")


@dataclass(slots=True)
class ParsedEmail:
    """A parsed email message.

    Attributes:
        message_id: The RFC 5322 Message-ID, when present. This is the
            stable identity we use to avoid processing the same email
            twice.
        subject: The subject line, with reply/forward prefixes removed.
        sender: The From address.
        recipients: The To/Cc addresses.
        body: The body of the first message, with the quoted chain removed.
        body_is_html: True when `body` is still HTML and needs rendering.
        attachments: Paths to extracted attachments, on disk.
        sent_at: The Date header, parsed.
        headers: The remaining headers, for anything we have not modelled.
    """

    message_id: str
    subject: str
    sender: str
    recipients: list[str] = field(default_factory=list)
    body: str = ""
    body_is_html: bool = False
    attachments: list[Path] = field(default_factory=list)
    sent_at: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


def _clean_subject(subject: str) -> str:
    """Strip reply and forward prefixes."""
    return _SUBJECT_PREFIX.sub("", subject or "").strip()


def strip_quoted_chain(body: str) -> str:
    """Remove the quoted reply chain from an email body.

    Kept separate and pure so it can be unit tested against a corpus of
    real-world quoting styles (Outlook, Gmail, Apple Mail, Thunderbird),
    which disagree about where the chain starts.

    The rule is deliberately simple: the chain starts at the first line
    that looks like a quote, and everything from there is dropped. Every
    real quoting style has a marker on the first line of the quoted
    block, and guessing more cleverly than that gets it wrong on the
    style you did not think of.
    """
    if not body:
        return ""

    lines = body.splitlines()
    kept: list[str] = []

    for line in lines:
        if any(marker.search(line) for marker in _QUOTE_MARKERS):
            break
        kept.append(line)

    # Trim a trailing signature block. It is the sender's own, it is not
    # part of the message, and it often carries a direct phone number.
    for index in range(len(kept) - 1, -1, -1):
        if _SIGNATURE_MARKER.match(kept[index]):
            kept = kept[:index]
            break

    return "\n".join(kept).strip()


def _html_to_text(html: str) -> str:
    """A deliberately small HTML → text conversion.

    Not a full parser. An email body is mostly paragraphs and links, and
    the thing that matters is that the text is readable and that a
    personal detail in a link is not lost.
    """
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)

    import html as html_module

    text = html_module.unescape(text)

    # Collapse the runs of spaces that tag-stripping leaves behind, but
    # keep the line breaks.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _body_text(message: EmailMessage) -> tuple[str, bool]:
    """The body of the first message. Returns (text, is_html)."""
    plain: list[str] = []
    html: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if payload is None:
                continue
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if content_type == "text/plain":
                plain.append(decoded)
            else:
                html.append(decoded)
    else:
        try:
            payload = message.get_payload(decode=True)
        except Exception:
            payload = None
        if payload:
            decoded = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
            if message.get_content_type() == "text/html":
                html.append(decoded)
            else:
                plain.append(decoded)

    if plain:
        return "\n".join(plain), False
    if html:
        return _html_to_text("\n".join(html)), True
    return "", False


def _extract_attachments(message: EmailMessage) -> list[Path]:
    """Write the attachments to a temporary directory and return their paths."""
    attachments: list[Path] = []
    if not message.is_multipart():
        return attachments

    out_dir = Path(tempfile.mkdtemp(prefix="inkwell-attachments-"))

    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue

        # Never let an attachment escape the directory it was written to.
        safe_name = Path(filename).name or f"attachment-{index}"
        path = out_dir / safe_name
        path.write_bytes(payload)
        attachments.append(path)

    return attachments


def parse_email(source: str | Path | bytes) -> ParsedEmail:
    """Parse an email from a file, a raw RFC 5322 message, or bytes.

    Returns:
        A `ParsedEmail`. Attachments are extracted alongside the body so
        the document parser can handle them with the same code path.
    """
    if isinstance(source, bytes):
        raw = source
    elif isinstance(source, Path) or (isinstance(source, str) and "\n" not in source):
        raw = Path(source).read_bytes()
    else:
        raw = str(source).encode("utf-8", errors="replace")

    message = message_from_bytes(raw, policy=policy.default)

    body, is_html = _body_text(message)
    body = strip_quoted_chain(body)

    recipients: list[str] = []
    for header in ("To", "Cc"):
        for value in message.get_all(header, []):
            recipients.extend(part.strip() for part in str(value).split(",") if part.strip())

    headers = {str(key): str(value) for key, value in message.items()}

    return ParsedEmail(
        message_id=str(message.get("Message-ID", "") or ""),
        subject=_clean_subject(str(message.get("Subject", "") or "")),
        sender=str(message.get("From", "") or ""),
        recipients=recipients,
        body=body,
        body_is_html=is_html,
        attachments=_extract_attachments(message),
        sent_at=str(message.get("Date", "") or "") or None,
        headers=headers,
    )
