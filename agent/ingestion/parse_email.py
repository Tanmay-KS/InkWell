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

from dataclasses import dataclass, field
from pathlib import Path


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


def parse_email(source: str | Path | bytes) -> ParsedEmail:
    """Parse an email from a file, a raw RFC 5322 message, or bytes.

    Phase 1 implementation: use Python's stdlib `email` module, strip the
    quoted chain, and write attachments to a temporary directory.

    Returns:
        A `ParsedEmail`. Attachments are extracted alongside the body so the
        document parser can handle them with the same code path.
    """
    raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")


def strip_quoted_chain(body: str) -> str:
    """Remove the quoted reply chain from an email body.

    Kept separate and pure so it can be unit tested against a corpus of
    real-world quoting styles (Outlook, Gmail, Apple Mail, Thunderbird),
    which disagree about where the chain starts.

    Phase 1 implementation.
    """
    raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")
