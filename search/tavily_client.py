"""Tavily client — search, routed through the privacy router.

Phase 2 — Person A.

Tavily is the one third-party service the agent talks to, so it is the one
place where personal details can escape through a query string. Two
independent controls cover it:

1. **This client** redacts the query before it leaves the process, using
   `privacy_router.redact`. It knows the query is personal and so it
   treats it as personal.
2. **The OpenShell supervisor middleware** redacts the request body on the
   way out, whatever produced it. It does not know the query is personal,
   so it does not have to.

The second is the real control. The first exists so that the query is
redacted *before* it reaches the network at all, which means the raw
personal detail never even exists in a socket buffer, and so that the
leak test has a clean in-process hook to instrument.

Tavily has two other ways to carry personal detail out, and both are
handled here rather than left to chance:

* `include_raw_content` returns the full cleaned HTML of every result.
  A page about "cancel my car insurance" can contain a policy number in
  the body. It is off by default and stays off.
* `include_answer` makes Tavily generate an answer server-side from your
  query. The query is echoed into a third-party model. It is off by
  default and stays off.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from privacy_router.egress import OutboundRequest, Transport
from privacy_router.redact import local_redact

__all__ = ["SearchResult", "TavilyClient", "TavilyError", "build_query"]

TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com")


def build_query(
    *,
    provider: str = "",
    kind: str = "",
    reference: str = "",
    holder: str = "",
) -> str:
    """The query an agent would actually type to find out how to act.

    It quotes the reference and the holder because that is what makes a
    search specific: "how do I cancel *my* policy" returns a generic help
    page, and "Lakeshore … 0847-4851 Devin Sullivan" returns the right
    one. That is also exactly why the query is the leak vector — the
    details that make it useful are the details that must not leave.

    Kept as a pure function of plain strings so the leak test can build
    the same query in both conditions and vary only the router.
    """
    parts = [provider or "the provider"]
    if kind:
        parts.append(kind)
    parts.append("cancellation policy")
    if reference:
        parts.append(reference)
    if holder:
        parts.append(holder)
    return " ".join(part for part in parts if part)


class TavilyError(RuntimeError):
    """A Tavily request failed."""


@dataclass(slots=True)
class SearchResult:
    """One search result."""

    title: str
    url: str
    content: str
    score: float = 0.0
    raw_content: str | None = None
    published_date: str | None = None


@dataclass(slots=True)
class TavilyClient:
    """A thin, redaction-aware Tavily client.

    Attributes:
        api_key: The Tavily API key. Inside the sandbox this is an opaque
            placeholder that OpenShell substitutes at the network boundary.
        timeout: Per-request timeout in seconds.
        client: The underlying HTTP client, injectable for tests.
        transport: Where the request goes. When given, it replaces the
            HTTP call entirely — which is how the leak test captures the
            query without sending it.
    """

    api_key: str
    timeout: float = 30.0
    client: httpx.Client | None = field(default=None, repr=False)
    transport: Transport | None = field(default=None, repr=False)

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        include_domains: list[str] | None = None,
        redact: bool = True,
    ) -> list[SearchResult]:
        """Run a search.

        The query is redacted before it is sent. The response is not
        redacted — it is already third-party content, and redacting it
        would remove the very facts the user asked for.

        Args:
            query: The search query. Treated as personal data by default.
            max_results: How many results to return (Tavily caps this at 20).
            search_depth: "basic", "fast", "ultra-fast", or "advanced".
            include_domains: Restrict results to these domains.
            redact: Set False only for a query you know contains nothing
                personal — the router's own middleware will still redact
                the request body, so this is an optimisation, not a
                control.

        Returns:
            The results, in Tavily's ranking order.

        Raises:
            TavilyError: On a transport or API error.
        """
        if not query.strip():
            raise ValueError("query must not be empty")

        outbound = query
        if redact:
            outbound = local_redact(query).text

        payload: dict[str, Any] = {
            "query": outbound,
            "max_results": max_results,
            "search_depth": search_depth,
            # Off by default and deliberately left off: see the module
            # docstring. These two flags are how a personal detail escapes
            # a search without ever appearing in the query string.
            "include_raw_content": False,
            "include_answer": False,
        }
        if include_domains:
            payload["include_domains"] = include_domains

        # Every outbound query crosses the egress seam, so the leak test
        # sees it. `payload=outbound` is what actually leaves — not the
        # `query` argument, which may have been redacted on the way in.
        request = OutboundRequest(
            kind="lookup_call",
            payload=outbound,
            detail=f"tavily search (max_results={max_results}, depth={search_depth})",
        )
        body = self._send(request, payload)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TavilyError(f"Tavily returned something that is not JSON: {body[:200]!r}") from exc

        results: list[SearchResult] = []
        for item in data.get("results", []):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    content=item.get("content", ""),
                    score=float(item.get("score", 0.0)),
                    raw_content=item.get("raw_content"),
                    published_date=item.get("published_date"),
                )
            )
        return results

    def _send(self, request: OutboundRequest, payload: dict[str, Any]) -> str:
        """Put the request on the wire, or hand it to the transport.

        When a transport is set there is no network at all: that is the
        leak test's mode, and the reason the measurement is free and
        reproducible.
        """
        if self.transport is not None:
            return self.transport(request)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self.client is not None:
                response = self.client.post(
                    f"{TAVILY_BASE_URL}/search", json=payload, headers=headers
                )
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        f"{TAVILY_BASE_URL}/search", json=payload, headers=headers
                    )
        except httpx.HTTPError as exc:
            raise TavilyError(f"Tavily request failed: {exc}") from exc

        if response.status_code != 200:
            raise TavilyError(f"Tavily returned {response.status_code}: {response.text[:200]}")
        return response.text

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
