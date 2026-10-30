"""Mail for InkWell: Resend, SMTP and IMAP.

Receiving has two independent paths — Resend's API/webhook and a real
IMAP mailbox — and both end in the same store, redaction and graph.
Sending has two transports, Resend and SMTP, and both are only called
after the user approves an outbound action.
"""

from agent.mail.config import ImapSettings, SmtpSettings
from agent.mail.imap import ImapClient, ImapError, ParsedMessage, ingest_parsed, parse_message
from agent.mail.inbound import handle_event, handle_webhook, process_attachment_bytes, redact_body
from agent.mail.resend import (
    ResendClient,
    ResendError,
    WebhookVerificationError,
    verify_webhook,
)
from agent.mail.smtp import SentMail, SmtpClient, SmtpError, build_message
from agent.mail.store import MailStore

__all__ = [
    "ImapClient",
    "ImapError",
    "ImapSettings",
    "MailStore",
    "ParsedMessage",
    "ResendClient",
    "ResendError",
    "SentMail",
    "SmtpClient",
    "SmtpError",
    "SmtpSettings",
    "WebhookVerificationError",
    "build_message",
    "handle_event",
    "handle_webhook",
    "ingest_parsed",
    "parse_message",
    "process_attachment_bytes",
    "redact_body",
    "verify_webhook",
]
