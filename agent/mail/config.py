"""Connection settings for the SMTP and IMAP transports.

Both transports read one account from the environment. The names are
prefixed rather than shared so a household can send through one provider
and read through another, but `INKWELL_MAIL_USER` / `INKWELL_MAIL_PASSWORD`
act as a fallback for both.

Security defaults are deliberately strict:

* IMAP is SSL on 993. `INKWELL_IMAP_STARTTLS=1` switches to upgrade on 143.
* SMTP is STARTTLS on 587. `INKWELL_SMTP_SSL=1` switches to implicit TLS on
  465.
* a configuration with no TLS at all is refused unless
  `INKWELL_*_INSECURE=1` is set explicitly, which exists for a local test
  server and nothing else.

Nothing here connects to anything. The settings are inert until a client
is asked to talk.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

_TRUE = {"1", "true", "yes", "on"}


def _flag(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, not {raw!r}") from exc


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, not {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class ImapSettings:
    """How to reach one mailbox for reading."""

    host: str = ""
    port: int = 993
    user: str = ""
    password: str = field(default="", repr=False)
    mailbox: str = "INBOX"
    ssl: bool = True
    starttls: bool = False
    insecure: bool = False
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    @property
    def account(self) -> str:
        return f"imap:{self.user}@{self.host}"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ImapSettings:
        env = os.environ if environ is None else environ
        ssl = _flag(env, "INKWELL_IMAP_SSL", default=True)
        starttls = _flag(env, "INKWELL_IMAP_STARTTLS", default=False)
        user = (env.get("INKWELL_IMAP_USER") or env.get("INKWELL_MAIL_USER") or "").strip()
        password = env.get("INKWELL_IMAP_PASSWORD") or env.get("INKWELL_MAIL_PASSWORD") or ""
        return cls(
            host=(env.get("INKWELL_IMAP_HOST") or "").strip(),
            port=_int(env, "INKWELL_IMAP_PORT", 993 if ssl else 143),
            user=user,
            password=password,
            mailbox=(env.get("INKWELL_IMAP_MAILBOX") or "INBOX").strip() or "INBOX",
            ssl=ssl,
            starttls=starttls,
            insecure=_flag(env, "INKWELL_IMAP_INSECURE", default=False),
            timeout=_float(env, "INKWELL_IMAP_TIMEOUT", 30.0),
        )

    def validate(self) -> None:
        if not self.configured:
            raise ValueError("IMAP is not configured: set INKWELL_IMAP_HOST/USER/PASSWORD")
        if self.ssl and self.starttls:
            raise ValueError("choose INKWELL_IMAP_SSL or INKWELL_IMAP_STARTTLS, not both")
        if not (self.ssl or self.starttls or self.insecure):
            raise ValueError(
                "refusing plaintext IMAP; enable SSL/STARTTLS or set INKWELL_IMAP_INSECURE=1"
            )


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """How to send one message through an outbound server."""

    host: str = ""
    port: int = 587
    user: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""
    ssl: bool = False
    starttls: bool = True
    insecure: bool = False
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    @property
    def from_address(self) -> str:
        return self.sender or self.user

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SmtpSettings:
        env = os.environ if environ is None else environ
        ssl = _flag(env, "INKWELL_SMTP_SSL", default=False)
        starttls = _flag(env, "INKWELL_SMTP_STARTTLS", default=not ssl)
        user = (env.get("INKWELL_SMTP_USER") or env.get("INKWELL_MAIL_USER") or "").strip()
        password = env.get("INKWELL_SMTP_PASSWORD") or env.get("INKWELL_MAIL_PASSWORD") or ""
        return cls(
            host=(env.get("INKWELL_SMTP_HOST") or "").strip(),
            port=_int(env, "INKWELL_SMTP_PORT", 465 if ssl else 587),
            user=user,
            password=password,
            sender=(env.get("INKWELL_SMTP_FROM") or "").strip(),
            ssl=ssl,
            starttls=starttls,
            insecure=_flag(env, "INKWELL_SMTP_INSECURE", default=False),
            timeout=_float(env, "INKWELL_SMTP_TIMEOUT", 30.0),
        )

    def validate(self) -> None:
        if not self.configured:
            raise ValueError("SMTP is not configured: set INKWELL_SMTP_HOST/USER/PASSWORD")
        if self.ssl and self.starttls:
            raise ValueError("choose INKWELL_SMTP_SSL or INKWELL_SMTP_STARTTLS, not both")
        if not (self.ssl or self.starttls or self.insecure):
            raise ValueError(
                "refusing plaintext SMTP; enable SSL/STARTTLS or set INKWELL_SMTP_INSECURE=1"
            )
