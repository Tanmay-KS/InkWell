"""Resend API client and webhook verification.

Resend's receiving product delivers an `email.received` webhook that carries
metadata only. The body and attachments must be fetched with the same API key
afterwards, which is why every inbound path here needs a full-access key: a
send-only key can deliver mail but cannot read it back.

The client is deliberately small and uses `httpx` (already a dependency)
rather than the Resend SDK. That keeps the surface we have to trust to the
three endpoints this product uses, and it makes the client trivially
replaceable in tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlparse

import httpx

API_BASE = "https://api.resend.com"
DEFAULT_TIMEOUT = 20.0
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
WEBHOOK_TOLERANCE_SECONDS = 5 * 60


class ResendError(RuntimeError):
    """A Resend API call failed."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WebhookVerificationError(ValueError):
    """The request did not carry a valid Resend (Svix) signature."""


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return ""


def verify_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify a Resend webhook and return its parsed JSON body.

    Resend signs webhooks with Svix. The signature covers
    `"{svix-id}.{svix-timestamp}.{raw_body}"` and is an HMAC-SHA256 of that
    string, base64-encoded, keyed with the base64-decoded `whsec_...` secret.
    The raw bytes matter: re-serialising the JSON changes the signature.
    """
    if not secret:
        raise WebhookVerificationError("no webhook secret configured")

    svix_id = _header(headers, "svix-id")
    timestamp = _header(headers, "svix-timestamp")
    signatures = _header(headers, "svix-signature")
    if not svix_id or not timestamp or not signatures:
        raise WebhookVerificationError("missing Svix headers")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("invalid Svix timestamp") from exc

    current = int(time.time()) if now is None else now
    if abs(current - sent_at) > tolerance:
        raise WebhookVerificationError("stale webhook timestamp")

    encoded_secret = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(encoded_secret, validate=True)
    except ValueError as exc:
        raise WebhookVerificationError("invalid webhook secret") from exc

    signed = svix_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    matched = False
    for candidate in signatures.split():
        version, _, signature = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            matched = True
    if not matched:
        raise WebhookVerificationError("signature mismatch")

    try:
        event = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookVerificationError("payload is not JSON") from exc
    if not isinstance(event, dict):
        raise WebhookVerificationError("payload is not an object")
    return event


def _is_public_https(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local)


class ResendClient:
    """The three Resend calls InkWell needs."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key or os.environ.get("RESEND_API_KEY", "")).strip()
        self.timeout = timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _client(self, *, follow_redirects: bool = False) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=follow_redirects,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.api_key:
            raise ResendError("RESEND_API_KEY is not set")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with self._client() as client:
                response = client.request(
                    method,
                    f"{API_BASE}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise ResendError(f"resend request failed ({type(exc).__name__})") from exc

        if response.status_code >= 400:
            try:
                detail = response.json()
                message = detail.get("message") if isinstance(detail, dict) else None
            except ValueError:
                message = None
            raise ResendError(
                f"resend {response.status_code}: {message or 'request failed'}",
                status=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def get_received_email(self, email_id: str) -> dict[str, Any]:
        """Fetch the full received email, including body and headers."""
        data = self._request("GET", f"/emails/receiving/{quote(email_id, safe='')}")
        return data if isinstance(data, dict) else {}

    def list_attachments(self, email_id: str) -> list[dict[str, Any]]:
        """List attachment metadata, including signed download URLs."""
        data = self._request("GET", f"/emails/receiving/{quote(email_id, safe='')}/attachments")
        rows = data.get("data") if isinstance(data, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def download(self, url: str) -> bytes:
        """Download one attachment from a Resend signed URL, with a size cap."""
        if not _is_public_https(url):
            raise ResendError("attachment URL is not a public https URL")

        chunks: list[bytes] = []
        total = 0
        try:
            with self._client(follow_redirects=True) as client, client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise ResendError(
                        f"attachment download failed ({response.status_code})",
                        status=response.status_code,
                    )
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ResendError("attachment exceeds the 15 MiB limit")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ResendError(f"attachment download failed ({type(exc).__name__})") from exc
        return b"".join(chunks)

    def send(
        self,
        *,
        sender: str,
        to: list[str],
        subject: str,
        text: str | None = None,
        html: str | None = None,
        reply_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send an email. Only called after the user approves an action."""
        payload: dict[str, Any] = {"from": sender, "to": to, "subject": subject}
        if text:
            payload["text"] = text
        if html:
            payload["html"] = html
        if reply_to:
            payload["reply_to"] = reply_to
        data = self._request("POST", "/emails", json_body=payload)
        return data if isinstance(data, dict) else {}

    def list_webhooks(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/webhooks")
        rows = data.get("data") if isinstance(data, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def list_domains(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/domains")
        rows = data.get("data") if isinstance(data, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def list_received_emails(self, limit: int = 10) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/emails/receiving",
            params={"limit": max(1, min(int(limit), 100))},
        )
        rows = data.get("data") if isinstance(data, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def create_webhook(self, endpoint: str, events: list[str]) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/webhooks",
            json_body={"endpoint": endpoint, "events": events},
        )
        return data if isinstance(data, dict) else {}
