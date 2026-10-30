"""Resend-backed mail for InkWell."""

from agent.mail.inbound import handle_event, handle_webhook
from agent.mail.resend import (
    ResendClient,
    ResendError,
    WebhookVerificationError,
    verify_webhook,
)
from agent.mail.store import MailStore

__all__ = [
    "MailStore",
    "ResendClient",
    "ResendError",
    "WebhookVerificationError",
    "handle_event",
    "handle_webhook",
    "verify_webhook",
]
