"""Search integrations.

Phase 2 — Person A.

`search/tavily_client.py` is the only module here. Every outbound search
goes through it, and it redacts before the request leaves the process.

The reason this exists rather than using Hermes's built-in Tavily backend
directly: Hermes can call Tavily on its own, and a redaction hook inside
our client would not see those calls. The OpenShell supervisor middleware
does see them — which is why the middleware, not this client, is the
control. This client is the in-process half of the defence.
"""

from search.tavily_client import SearchResult, TavilyClient, TavilyError, build_query

__all__ = ["SearchResult", "TavilyClient", "TavilyError", "build_query"]
