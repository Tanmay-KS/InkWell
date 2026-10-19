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

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from privacy_router.redact import local_redact

__all__ = ["SearchResult", "TavilyClient", "TavilyError"]

TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com")


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
    """

    api_key: str
    timeout: float = 30.0
    client: httpx.Client | None = field(default=None, repr=False)

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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        client = self.client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                f"{TAVILY_BASE_URL}/search", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise TavilyError(f"Tavily request failed: {exc}") from exc

        if response.status_code != 200:
            raise TavilyError(
                f"Tavily returned {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
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

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
