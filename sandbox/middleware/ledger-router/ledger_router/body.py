"""Turn an HTTP request body into the body that may leave the sandbox.

The router is not a text filter bolted onto a socket. Token Factory and
Tavily both use JSON, and redacting the serialized JSON as text is how a
router breaks a request: a placeholder containing a quote, or a match
inside a model ID, turns a valid body into a 400.

So the body is handled by destination:

* Token Factory — redact only `messages[*].content`, and leave the model,
  the token budget, the tool definitions and every other field exactly as
  they were.
* Tavily — redact only `query`, and report the flags that turn a lookup
  into a third-party model echo.
* Anything else — redact the raw text only if the policy says the host or
  model is in scope. A body that cannot be decoded is passed through
  unchanged and reported as binary; the guard decides what to do with it.

The module never logs a body. It returns a SHA-256 and category counts, so
an audit entry can prove *that* a redaction happened without becoming a
second copy of the data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ledger_router.config import RouterConfig
from privacy_router.egress import Transport
from privacy_router.redact import (
    RedactionResult,
    contains_personal_detail,
    local_redact,
    redact,
)

__all__ = ["BodyResult", "process_body"]

TOKEN_FACTORY_HOST = "api.tokenfactory.nebius.com"
TAVILY_HOST = "api.tavily.com"


@dataclass(slots=True)
class BodyResult:
    """What the body processor found and what it would send instead."""

    body: bytes
    categories: list[str] = field(default_factory=list)
    model: str | None = None
    has_image: bool = False
    flags: list[str] = field(default_factory=list)
    changed: bool = False
    binary: bool = False
    escalated: bool = False
    redaction_count: int = 0

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def _redact_text(
    text: str,
    *,
    config: RouterConfig,
    names: list[str],
    transport: Transport | None,
) -> RedactionResult:
    """Run the deterministic pass, and optionally the Nano escalation."""
    if (
        config.escalate
        and transport is not None
        and len(text.encode("utf-8", "ignore")) <= config.max_escalation_bytes
    ):
        return redact(text, transport=transport, names=names)
    return local_redact(text, names=names)


def _categories(text: str, names: list[str]) -> list[str]:
    return sorted(category.value for category in contains_personal_detail(text, names=names))


def _process_json(
    document: dict[str, Any],
    *,
    host: str,
    config: RouterConfig,
    names: list[str],
    transport: Transport | None,
    result: BodyResult,
) -> dict[str, Any]:
    """Mutate only the fields that are allowed to carry user text."""
    changed = False
    categories: set[str] = set()

    if host == TAVILY_HOST:
        flags = [
            flag
            for flag in config.forbidden_flags
            if document.get(flag) is True
        ]
        result.flags = sorted(set(flags))

        query = document.get("query")
        if isinstance(query, str) and query:
            categories.update(_categories(query, names))
            outcome = _redact_text(query, config=config, names=names, transport=transport)
            result.escalated = result.escalated or outcome.escalated
            result.redaction_count += len(outcome.redactions)
            if outcome.text != query:
                document["query"] = outcome.text
                changed = True

    elif host == TOKEN_FACTORY_HOST:
        model = document.get("model")
        if isinstance(model, str):
            result.model = model

        # The OCR model is a Super-tier model, so it can be in
        # `redact_models` and still legitimately receive an image. The
        # image itself is not redactable; the guard decides whether the
        # model is the approved reader.
        messages = document.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    categories.update(_categories(content, names))
                    outcome = _redact_text(
                        content, config=config, names=names, transport=transport
                    )
                    result.escalated = result.escalated or outcome.escalated
                    result.redaction_count += len(outcome.redactions)
                    if outcome.text != content:
                        message["content"] = outcome.text
                        changed = True
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        part_type = part.get("type")
                        if part_type == "image_url" or "image_url" in part:
                            result.has_image = True
                            continue
                        text = part.get("text")
                        if isinstance(text, str):
                            categories.update(_categories(text, names))
                            outcome = _redact_text(
                                text, config=config, names=names, transport=transport
                            )
                            result.escalated = result.escalated or outcome.escalated
                            result.redaction_count += len(outcome.redactions)
                            if outcome.text != text:
                                part["text"] = outcome.text
                                changed = True

    else:
        # An allowed host with no schema of its own. Redact the string
        # fields, but only when the policy puts it in scope.
        if config.redacts_host(host):
            for key, value in list(document.items()):
                if isinstance(value, str):
                    categories.update(_categories(value, names))
                    outcome = _redact_text(
                        value, config=config, names=names, transport=transport
                    )
                    result.escalated = result.escalated or outcome.escalated
                    result.redaction_count += len(outcome.redactions)
                    if outcome.text != value:
                        document[key] = outcome.text
                        changed = True

    result.categories = sorted(categories)
    result.changed = changed
    return document


def process_body(
    *,
    host: str,
    path: str,
    raw: bytes,
    config: RouterConfig,
    names: list[str] | None = None,
    transport: Transport | None = None,
) -> BodyResult:
    """Redact the parts of `raw` that may carry personal detail.

    Args:
        host: Destination host.
        path: Request path, for the audit detail.
        raw: The request body exactly as the sandbox produced it.
        config: The policy for this middleware stage.
        names: Household names, from the host-side names file.
        transport: The egress transport for the optional Nano escalation.
            `None` means deterministic-only, which is the default.

    Returns:
        A `BodyResult` whose `body` is what should be forwarded.
    """
    names = names or []
    destination = host.strip().lower()
    result = BodyResult(body=raw)

    if not raw:
        return result

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        result.binary = True
        return result

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None

    if isinstance(document, dict):
        in_scope = (
            destination in {TOKEN_FACTORY_HOST, TAVILY_HOST}
            or config.redacts_host(destination)
        )
        if in_scope:
            document = _process_json(
                document,
                host=destination,
                config=config,
                names=names,
                transport=transport,
                result=result,
            )
            encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
            result.body = encoded.encode("utf-8")
        else:
            # Not a host with a known schema. Still report what is in it,
            # so the guard can make the decision, but do not rewrite it.
            result.categories = _categories(text, names)
        return result

    # Not JSON. Rewrite it only if the host is explicitly in scope.
    if config.redacts_host(destination):
        outcome = _redact_text(text, config=config, names=names, transport=transport)
        result.categories = sorted(category.value for category in outcome.categories)
        result.escalated = outcome.escalated
        result.redaction_count = len(outcome.redactions)
        if outcome.text != text:
            result.body = outcome.text.encode("utf-8")
            result.changed = True
    else:
        result.categories = _categories(text, names)
    return result
