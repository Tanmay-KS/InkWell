"""The egress boundary.

Phase 2 — the seam the privacy router sits on.

Every byte that leaves the sandbox does so through a `transport`, and
every transport takes an `OutboundRequest`. There are three outbound calls
in this system, and all three can carry source text:

    kind          destination                   when
    ocr_call      api.tokenfactory.nebius.com   a page local OCR cannot read
    redact_call   api.tokenfactory.nebius.com   the router escalating residual spans
    lookup_call   api.tavily.com                the agent asking how to act

The leak test (`eval/leak_test.py`) measures what each one carries. One
seam for all of them means the test cannot miss a call site: a new
outbound call has to name a kind here, or it does not exist.

Two transports ship with it:

* `capturing_transport()` records the request and sends **nothing**. It is
  what the leak test uses, so measuring the leak rate costs nothing, makes
  no network calls, and gives the same answer every run.
* `http_transport()` really posts a chat completion to Token Factory.

The payload is withheld from `__repr__` and from the default serialised
form. In the router-OFF condition it holds the raw document text, and a
payload that turns up in a traceback, a log line, or a notebook cell is
the leak this module exists to prevent.
"""

from __future__ import annotations

import contextvars
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "KIND_DESTINATION",
    "TAVILY_HOST",
    "TOKEN_FACTORY_HOST",
    "EgressCapture",
    "OutboundRequest",
    "Transport",
    "active_capture",
    "capture",
    "capturing_transport",
    "dump_requests",
    "http_transport",
]

TOKEN_FACTORY_HOST = "api.tokenfactory.nebius.com"
TAVILY_HOST = "api.tavily.com"

# What each kind of outbound call is aimed at. The policy in
# `sandbox/policy.yaml` allows exactly these hosts, so a request that names
# a destination outside this map is a bug, not a new capability.
KIND_DESTINATION: dict[str, str] = {
    "ocr_call": TOKEN_FACTORY_HOST,
    "redact_call": TOKEN_FACTORY_HOST,
    "lookup_call": TAVILY_HOST,
}


@dataclass(slots=True)
class OutboundRequest:
    """One request that would leave the sandbox.

    Attributes:
        kind: One of `KIND_DESTINATION`. The leak test groups by this.
        payload: **The user data the request carries.** This is what gets
            scored for personal detail. With the router off it is raw
            document text, so it is never printed by default.
        prompt: The instruction that wraps the payload. Not personal, and
            not scored — the model has to be told what to do with the
            text, and the instructions are ours.
        model: The model the request is aimed at, when it is a model call.
        destination: The host. Defaults from `kind`.
        detail: A short human note for the audit trail.
    """

    kind: str
    payload: str
    prompt: str = ""
    model: str = ""
    destination: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.destination:
            self.destination = KIND_DESTINATION.get(self.kind, "")

    def __repr__(self) -> str:
        # The payload is the point of the module, so it does not get a
        # repr. Debugging an egress bug by printing the leak is not
        # debugging.
        return (
            f"OutboundRequest(kind={self.kind!r}, "
            f"destination={self.destination!r}, "
            f"payload=<{len(self.payload)} chars withheld>, "
            f"model={self.model!r})"
        )

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        """The request as a plain dict.

        The payload is opt-in. A JSON log of every outbound request is
        useful; a JSON log of every outbound payload would be a second
        copy of the leak.
        """
        data: dict[str, Any] = {
            "kind": self.kind,
            "destination": self.destination,
            "model": self.model,
            "detail": self.detail,
            "payload_chars": len(self.payload),
        }
        if include_payload:
            data["payload"] = self.payload
        return data


Transport = Callable[[OutboundRequest], str]
"""Sends a request and returns the answer body. The only way out."""


@dataclass(slots=True)
class EgressCapture:
    """Every request that passed the boundary while it was active."""

    requests: list[OutboundRequest] = field(default_factory=list)

    def record(self, request: OutboundRequest) -> OutboundRequest:
        self.requests.append(request)
        return request

    def by_kind(self) -> dict[str, list[OutboundRequest]]:
        grouped: dict[str, list[OutboundRequest]] = {}
        for request in self.requests:
            grouped.setdefault(request.kind, []).append(request)
        return grouped

    def __len__(self) -> int:
        return len(self.requests)

    def __iter__(self) -> Iterator[OutboundRequest]:
        return iter(self.requests)


_active: contextvars.ContextVar[EgressCapture | None] = contextvars.ContextVar(
    "inkwell_egress_capture", default=None
)


@contextmanager
def capture() -> Iterator[EgressCapture]:
    """Record every outbound request made inside the block.

    A context var rather than a module global, so a test that runs a
    pipeline inside a capture does not also capture another test's calls.
    """
    sink = EgressCapture()
    token = _active.set(sink)
    try:
        yield sink
    finally:
        _active.reset(token)


def active_capture() -> EgressCapture | None:
    """The capture in force, if any."""
    return _active.get()


def capturing_transport(response: str = "") -> Transport:
    """A transport that records the request and sends nothing.

    This is the leak test's transport. It is deliberately the *only*
    thing the leak test needs: the measurement is of what would leave,
    not of what the far end would say, so the test never needs a network,
    an API key, or a budget.
    """
    canned = response

    def send(request: OutboundRequest) -> str:
        sink = active_capture()
        if sink is not None:
            sink.record(request)
        return canned

    return send


def http_transport(
    api_key: str | None = None,
    *,
    base_url: str | None = None,
    timeout: float = 60.0,
    max_tokens: int = 1024,
) -> Transport:
    """A transport that really posts a chat completion to Token Factory.

    Only Token Factory is supported here. Tavily is a different API shape;
    its live client lives in `search/tavily_client.py`, and leaving it out
    of this function keeps one transport from guessing at two schemas.
    """
    key = api_key or os.environ.get("NEBIUS_API_KEY", "")
    root = (base_url or os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")).rstrip("/")

    def send(request: OutboundRequest) -> str:
        import httpx

        if request.destination != TOKEN_FACTORY_HOST:
            raise ValueError(
                f"{request.kind} is aimed at {request.destination!r}; "
                "http_transport only speaks Token Factory chat completions"
            )
        if not key:
            raise RuntimeError("NEBIUS_API_KEY is not set — cannot send a model call")

        content = f"{request.prompt}\n\n{request.payload}" if request.prompt else request.payload
        body = {
            "model": request.model or os.environ.get("INKWELL_MODEL_REDACT", ""),
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{root}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
            )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        message = data["choices"][0]["message"]
        return message.get("content") or ""

    return send


def dump_requests(capture_sink: EgressCapture) -> str:
    """A request log safe to print — kinds and sizes, no payloads."""
    return json.dumps([r.to_dict() for r in capture_sink], indent=2)
