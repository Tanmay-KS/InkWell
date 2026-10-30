"""The one model client the role graph is allowed to use.

Upgrade Phase B (LangGraph).

Every model call in this module goes to **Nebius Token Factory**, using a
model ID pinned by `make pin-models` in `.env`:

    INKWELL_MODEL_NANO, INKWELL_MODEL_SUPER, INKWELL_MODEL_ULTRA, ...

There is no fallback provider. If the key or the pinned ID is missing,
`complete()` raises instead of quietly calling something else. That is the
whole point: a benchmark or a graph run that silently used a different
endpoint would invalidate the project's privacy and cost claims.

LangGraph itself pulls `langsmith` transitively through `langchain-core`.
Tracing is explicitly disabled in `agent/graph.py` before the import; this
module never reads or sets a LangSmith key.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_BASE_URL", "ModelReply", "NebiusClient", "load_api_key", "pinned_model"]

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


def load_api_key(explicit: str = "") -> str:
    """The key from the argument, the environment, or `.env`.

    Mirrors `eval/tokenfactory.py` so the shipped package does not depend on
    the eval scripts. `.env` is read by hand, with no import-time side
    effects.
    """
    if explicit:
        return explicit.strip()
    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("NEBIUS_API_KEY="):
                return stripped.split("=", 1)[1].strip().strip("'\"")
    return ""


def base_url() -> str:
    """The Token Factory base URL.

    `NEBIUS_BASE_URL` is honoured for the sandbox, where the host is bound
    by policy. It is not a way to point the graph at another provider.
    """
    return os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def pinned_model(role: str) -> str:
    """The model ID pinned for a role, or a clear error.

    Roles are `nano`, `super`, `ultra`, `redact`, `ocr`. The ID comes from
    `INKWELL_MODEL_<ROLE>`, which `make pin-models` writes from the live
    catalog. Guessing an ID here would defeat the point of pinning it.
    """
    name = f"INKWELL_MODEL_{role.upper()}"
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}="):
                return stripped.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError(
        f"{name} is not set; run `make pin-models` so the graph uses a catalog ID"
    )


@dataclass(slots=True)
class ModelReply:
    """One completion, with the numbers the trace records."""

    text: str
    model: str
    tier: str
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0


class NebiusClient:
    """A minimal chat-completions client for Token Factory."""

    def __init__(
        self,
        *,
        api_key: str = "",
        timeout: float = 90.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_key = load_api_key(api_key)
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.base_url = base_url()

    def available(self) -> bool:
        """Whether a live call has any chance of working."""
        return bool(self.api_key)

    def complete(
        self,
        role: str,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> ModelReply:
        """One chat completion against Token Factory.

        Raises `RuntimeError` when the key, the model ID, the transport or
        the response is unusable. There is no retry against another
        provider and no template fallback hidden inside this method.
        """
        import httpx

        if not self.api_key:
            raise RuntimeError("NEBIUS_API_KEY is not set; refusing to call a fallback provider")

        model = pinned_model(role)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature,
        }
        url = f"{self.base_url}/chat/completions"
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Token Factory transport failed: {type(exc).__name__}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            # The body is safe to include: Token Factory errors do not echo
            # the prompt, and a bare status is not diagnosable.
            raise RuntimeError(
                f"HTTP {response.status_code} from {url}: {response.text[:300]}"
            )

        try:
            payload = response.json()
            choice = payload["choices"][0]
            text = (choice.get("message") or {}).get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Token Factory returned an unreadable completion") from exc
        if not text.strip():
            finish = choice.get("finish_reason")
            if finish == "length":
                raise RuntimeError(
                    "Token Factory ran out of completion budget before answering; "
                    "raise max_tokens (this model reasons before it writes)"
                )
            raise RuntimeError("Token Factory returned an empty completion")

        usage = payload.get("usage") or {}
        return ModelReply(
            text=text.strip(),
            model=str(payload.get("model") or model),
            tier=role,
            latency_ms=latency_ms,
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
        )
