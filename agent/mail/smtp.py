"""Send mail through an SMTP account.

This is the outbound side of the same boundary the Resend client follows:
build the message, hand it to the provider, and return what the provider
said. Nothing here decides *whether* to send; that stays behind the
approval step, and the command line is the only caller today.

`EmailMessage` is used rather than hand-written headers so encoded words,
line folding and the multipart/alternative structure are handled by the
standard library. The message id is set explicitly so a copy can be
recorded in the local store and matched later.

Errors carry the host and the exception type, never the password. An
authentication failure from a server sometimes echoes the user name back,
which is acceptable; it does not echo the secret.
"""

from __future__ import annotations

import contextlib
import smtplib
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from agent.mail.config import SmtpSettings


class SmtpError(RuntimeError):
    """An SMTP conversation failed."""


@dataclass(slots=True)
class SentMail:
    """What one send produced."""

    message_id: str
    sender: str
    recipients: list[str]
    subject: str
    accepted: list[str]
    refused: dict[str, tuple[int, bytes]]
    transport: str = "smtp"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.message_id,
            "sender": self.sender,
            "to": self.recipients,
            "subject": self.subject,
            "accepted": self.accepted,
            "refused": {key: list(value) for key, value in self.refused.items()},
            "transport": self.transport,
        }


def build_message(
    *,
    sender: str,
    to: Iterable[str],
    subject: str,
    text: str | None = None,
    html: str | None = None,
    reply_to: Iterable[str] = (),
    headers: dict[str, str] | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    """Build the message without sending it.

    Exposed separately because it is the part worth testing and the part a
    future approval UI would show before the user clicks send.
    """
    recipients = [str(value).strip() for value in to if str(value).strip()]
    if not recipients:
        raise ValueError("at least one recipient is required")
    if not subject.strip():
        raise ValueError("a subject is required")
    if not text and not html:
        raise ValueError("a text or html body is required")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    domain = sender.rpartition("@")[2] or "inkwell.local"
    message["Message-ID"] = message_id or make_msgid(domain=domain)
    reply = [str(value).strip() for value in reply_to if str(value).strip()]
    if reply:
        message["Reply-To"] = ", ".join(reply)
    for name, value in (headers or {}).items():
        message[name] = value

    message.set_content(text or "")
    if html:
        message.add_alternative(html, subtype="html")
    return message


class SmtpClient:
    """One SMTP account, connected on demand."""

    def __init__(
        self,
        settings: SmtpSettings | None = None,
        *,
        sender: str | None = None,
        factory: Callable[[SmtpSettings], Any] | None = None,
    ) -> None:
        self.settings = settings or SmtpSettings.from_env()
        self.sender = sender or self.settings.from_address
        self._factory = factory

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def _connect(self) -> Any:
        self.settings.validate()
        if self._factory is not None:
            return self._factory(self.settings)

        context = ssl.create_default_context()
        try:
            if self.settings.ssl:
                return smtplib.SMTP_SSL(
                    self.settings.host,
                    self.settings.port,
                    timeout=self.settings.timeout,
                    context=context,
                )
            client = smtplib.SMTP(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout,
            )
            client.ehlo()
            if self.settings.starttls:
                client.starttls(context=context)
                client.ehlo()
            return client
        except (OSError, smtplib.SMTPException) as exc:
            raise SmtpError(
                f"could not connect to {self.settings.host}:{self.settings.port} "
                f"({type(exc).__name__})"
            ) from None

    @staticmethod
    def _close(client: Any) -> None:
        try:
            client.quit()
        except Exception:
            with contextlib.suppress(Exception):
                client.close()

    def _login(self, client: Any) -> None:
        if not self.settings.user:
            return
        try:
            client.login(self.settings.user, self.settings.password)
        except smtplib.SMTPAuthenticationError as exc:
            raise SmtpError(
                f"SMTP authentication failed for {self.settings.user} "
                f"(server said: {exc.smtp_code})"
            ) from None
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpError(f"SMTP login failed ({type(exc).__name__})") from None

    def verify(self) -> dict[str, Any]:
        """Connect, authenticate and disconnect without sending."""
        client = self._connect()
        try:
            self._login(client)
            code, detail = client.noop()
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpError(f"SMTP verify failed ({type(exc).__name__})") from None
        finally:
            self._close(client)
        return {
            "host": self.settings.host,
            "port": self.settings.port,
            "user": self.settings.user,
            "noop": int(code) if isinstance(code, int) else code,
            "detail": detail.decode("utf-8", "replace") if isinstance(detail, bytes) else detail,
        }

    def send(
        self,
        *,
        to: Iterable[str],
        subject: str,
        text: str | None = None,
        html: str | None = None,
        sender: str | None = None,
        reply_to: Iterable[str] = (),
        headers: dict[str, str] | None = None,
    ) -> SentMail:
        """Send one message. Only called after the user approves it."""
        from_address = sender or self.sender
        if not from_address:
            raise SmtpError("no sender address: set INKWELL_SMTP_FROM")
        message = build_message(
            sender=from_address,
            to=to,
            subject=subject,
            text=text,
            html=html,
            reply_to=reply_to,
            headers=headers,
        )
        recipients = [str(value).strip() for value in to if str(value).strip()]

        client = self._connect()
        try:
            self._login(client)
            refused = client.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            raise SmtpError(f"every recipient was refused: {sorted(exc.recipients)}") from None
        except smtplib.SMTPSenderRefused as exc:
            raise SmtpError(f"the sender was refused ({exc.smtp_code})") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpError(f"SMTP send failed ({type(exc).__name__})") from None
        finally:
            self._close(client)

        return SentMail(
            message_id=str(message["Message-ID"]),
            sender=from_address,
            recipients=recipients,
            subject=subject,
            accepted=[value for value in recipients if value not in refused],
            refused=refused or {},
        )
