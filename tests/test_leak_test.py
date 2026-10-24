"""Tests for the Phase 2 leak test.

The leak test produces the project's headline number, so the failure mode
that matters is not "the number is wrong" — it is "the number is right for
the wrong reason". A harness whose router-ON leg cannot fail would report
0% whatever the router did.

Two tests carry that weight:

* the OFF leg must leak, or the corpus and the query are not exercising
  the path at all;
* a router that does nothing must be caught, or 0% ON is meaningless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.leak_test import (
    CORPUS_TODAY,
    _capture_for,
    _documents,
    household_names,
    run_leak_test,
)
from privacy_router.egress import OutboundRequest, capture, capturing_transport

CORPUS = Path("eval/synthetic_docs")


@pytest.fixture(scope="module")
def corpus() -> list[Path]:
    if not CORPUS.exists():
        pytest.skip("synthetic corpus missing — run: make synth")
    paths = _documents(CORPUS, 3, scans=False)
    if not paths:
        pytest.skip("synthetic corpus is empty — run: make synth")
    return paths


def test_the_off_condition_leaks_and_the_on_condition_does_not(corpus):
    """The headline, at three documents.

    If the OFF leg ever stops leaking, the harness has stopped exercising
    the egress path — which is a broken test, not a privacy win.
    """
    results = run_leak_test(CORPUS, count=3)

    off = results["router_off"].headline
    on = results["router_on"].headline

    assert off.payloads == 3
    assert off.payloads_with_pii == 3
    assert off.leak_rate == 1.0

    assert on.payloads == 3
    assert on.payloads_with_pii == 0
    assert on.leak_rate == 0.0


def test_a_router_that_does_nothing_is_caught(corpus):
    """The negative control.

    `lambda text: text` is a router with no redaction in it. The ON leg
    must report a leak, because otherwise a 0% result could come from the
    router never having been called.
    """
    results = run_leak_test(CORPUS, count=3, router=lambda text: text)

    on = results["router_on"].headline
    assert on.payloads_with_pii == on.payloads == 3


def test_the_only_difference_between_conditions_is_the_placeholders(corpus):
    """Same document, same query — redacted or not.

    This is what makes the comparison honest: the harness does not send a
    vaguer question in the ON condition. It sends the same question with
    the personal detail masked.
    """
    path = corpus[0]
    names = household_names(CORPUS, [path])

    with _capture_for(path, router_on=False, names=names) as sink_off:
        off_payloads = [request.payload for request in sink_off if request.kind == "lookup_call"]

    with _capture_for(path, router_on=True, names=names) as sink_on:
        on_payloads = [request.payload for request in sink_on if request.kind == "lookup_call"]

    assert len(off_payloads) == len(on_payloads) == 1
    assert off_payloads[0] != on_payloads[0]
    assert "[ACCOUNT_1]" in on_payloads[0]
    assert "[NAME_1]" in on_payloads[0]
    # Everything that is not a placeholder is identical.
    assert off_payloads[0].replace(names[0], "[NAME_1]") != off_payloads[0]


def test_the_redaction_call_is_its_own_line_item(corpus):
    """The router's own egress is reported, and only when the router runs."""
    results = run_leak_test(CORPUS, count=3)

    off_redact = results["router_off"].line_items["redact_call"]
    on_redact = results["router_on"].line_items["redact_call"]

    assert off_redact.payloads == 0
    assert "not applicable" in off_redact.note

    assert on_redact.payloads == 3
    # What it carries is the locally-redacted text, not the source text.
    assert on_redact.payloads_with_pii == 0

    # And it is not in the headline: 3 payloads, not 6.
    assert results["router_on"].headline.payloads == 3


def test_an_ocr_call_counts_as_a_leak_whenever_it_happens(corpus):
    """An image payload is the document, so it is scored by kind.

    There is nothing to pattern-match in base64; the fact that it was sent
    is the finding.
    """
    from eval.leak_test import _score

    request = OutboundRequest(kind="ocr_call", payload="iVBORw0KGgo=")
    assert _score(request, []) == ["document_image"]

    lookup = OutboundRequest(kind="lookup_call", payload="a policy query")
    assert _score(lookup, []) == []


def test_the_capture_sends_nothing_and_keeps_the_payload_out_of_repr():
    """Two properties the harness depends on.

    The capture holds the payload so it can be scored; its repr does not,
    because a payload in a traceback is the leak the module prevents.
    """
    with capture() as sink:
        transport = capturing_transport("canned")
        answer = transport(OutboundRequest(kind="lookup_call", payload="Devin Sullivan"))

    assert answer == "canned"
    assert len(sink) == 1
    assert sink.requests[0].payload == "Devin Sullivan"
    assert "Devin Sullivan" not in repr(sink.requests[0])
    assert "Devin Sullivan" not in str(sink.requests[0].to_dict())


def test_the_reference_date_matches_the_corpus():
    """The corpus is generated against a fixed date.

    A leak test on a different date would compare against documents whose
    deadlines have moved, and the query is derived from the document.
    """
    assert CORPUS_TODAY.isoformat() == "2026-10-20"
