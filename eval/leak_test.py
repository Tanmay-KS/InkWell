
    
  
#!/usr/bin/env python3
"""The leak test — router ON vs OFF.

Phase 2 — Person B.

This is the project's headline number. The question it answers: *when the
agent goes looking for something on the web, how often does a personal
detail leave the sandbox?*

The method is deliberately blunt, because a number that cannot be argued
with is worth more than a number that is merely sophisticated.

1. Run every synthetic document through the pipeline.
2. Capture every outbound request the pipeline would make — the model
   prompt, the Tavily query, the vision fallback.
3. Score each captured payload for personal detail, using
   `privacy_router.redact.contains_personal_detail`.
4. Repeat with the router attached (ON) and not attached (OFF).
5. Report the rate in each condition, and the reduction.

**No request is sent.** Every call is captured at the egress seam
(`privacy_router/egress.py`), so the measurement costs nothing, needs no
API key, and gives the same answer on every run. The number is about what
*would* leave, which is the question — what the far end would answer is
irrelevant to it.

**The router ON/OFF toggle in the sandbox is the presence of the
`network_middlewares` entry in the sandbox policy** — not a flag inside our
code. That is the point: the ON condition has to be the real enforcement
path, or the measurement is of a toy. This run measures **the redaction
layer the middleware invokes**, which is the same `privacy_router.redact`.
It is the router's coverage, not proof that the gateway applied it; the
gateway ON leg needs the sandbox host and is reported separately (see
`docs/leak-test.md`).

What the number means, stated precisely:

* OFF — the fraction of outbound payloads containing a detectable personal
  detail, with no router in the path. The same pipeline, the same
  documents, the same query — only the privacy layer removed.
* ON — the same fraction, with `privacy_router.redact` in the path.
* The claim we can make is "personal details never reach a Super/Ultra
  tier or a third-party service through an outbound payload".

**What the number does not cover, and must not hide:** the redaction model
call is itself outbound traffic carrying the source text. It is reported
as its own line item, never folded into the headline. The architectural
defence for that is the ordering in `privacy_router/redact.py` — the
deterministic local pass runs first, and the escalation carries the text
*after* that pass.

An `ocr_call` is scored as a leak whenever it happens, because its payload
is the document image and an image is personal by definition. On this
corpus the local extractor reads every document, so no image is sent.

The harness also performs a separate reversibility self-check: resolving the
router-ON document's placeholders must reproduce the router-OFF document text
byte-for-byte. This is a comparability check, not part of the leak headline.
Retained-value coverage remains in `eval/corpus_redaction_audit.py` and its
unit tests; it is intentionally not duplicated in this captured-egress test.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.pipeline import process_document
from privacy_router.egress import (
    OutboundRequest,
    capture,
    capturing_transport,
)
from privacy_router.redact import contains_personal_detail, redact
from search.tavily_client import TavilyClient, build_query

__all__ = ["ConditionResult", "LeakStats", "LeakTestResults", "main", "run_leak_test"]

# The corpus is generated against a fixed date, so the test has to use the
# same one or the deadlines move and the queries change with them.
CORPUS_TODAY = date(2026, 10, 20)

# The calls that carry user data to a model or a third party. These are the
# headline.
HEADLINE_KINDS = ("ocr_call", "lookup_call")

# Every line item the harness knows how to measure. `redact_call` is here
# and deliberately not in the headline.

