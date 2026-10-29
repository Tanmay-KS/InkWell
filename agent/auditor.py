"""Auditor: a reviewer with a veto, not a supervisor.

Upgrade Phase B.

The Auditor receives the draft, the Verifier's facts, and the source spans
the draft is allowed to rely on. It returns one of two decisions:

    approve  — no issues found
    reject   — one or more issues, each with a code and a reason

It takes no other action. It never sends, saves, or rewrites. The
orchestrator owns the retry loop; this module owns the judgement.

**This build runs the Auditor as deterministic code.** That is a
deliberate, honest choice: the checks below are exact (an amount either
matches the Verifier or it does not), they run offline, and the planted
cases in `eval/auditor_cases/` are reproducible. A Nemotron Ultra review
can be layered on top later, but it is not claimed in the trace or the
README.

Checks:

1. every amount and date in the draft matches the Verifier's facts;
2. a factual assertion with no Verifier fact or source span is uncited;
3. personal detail beyond the user's own reference and name is rejected;
4. promised outcomes and legal-advice phrasing are rejected.

`revise_draft()` is the deterministic repair the Drafter uses between
rounds: it replaces a wrong amount or date with the verified one, drops an
uncited or promissory sentence, and masks unexpected personal detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from agent.actions.draft_actions import DraftAction
from agent.deadlines.flag_deadlines import normalize_date
from agent.memory.hermes_memory import HouseholdItem
from agent.verifier import VerifiedFacts
from privacy_router.redact import local_redact

__all__ = [
    "AuditDecision",
    "AuditIssue",
    "AuditResult",
    "audit_draft",
    "revise_draft",
]

CENT = 0.01

_AMOUNT = re.compile(r"(?:\$\s*)?([0-9][0-9,]*\.[0-9]{2})\b")
_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?\s*,?\s*\d{4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*\d{4}"
    r"|\d{1,2}[/.]\d{1,2}[/.]\d{4})\b"
)
_PERCENT = re.compile(r"\b\d+(?:\.\d+)?\s?%")
_PERIOD = re.compile(r"\b\d+[-\s](?:day|week|month|year)s?\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

_CLAIM_MARKERS = re.compile(
    r"\b(?:includes?|included|covered|coverage applies|entitled|"
    r"under the terms|according to|policy states|terms state|"
    r"guarantees?|warrants?|entitles?)\b",
    re.IGNORECASE,
)

_PROMISED_OUTCOME = re.compile(
    r"\b(?:"
    r"you will (?:get|receive|be given|be refunded|be credited|be reimbursed|be approved)"
    r"|we will (?:refund|credit|reimburse|pay|approve|grant)"
    r"|will be (?:refunded|credited|reimbursed|approved|granted)"
    r"|guarantee(?:d|s)?"
    r"|promise(?:d|s)?"
    r"|entitled to (?:a )?(?:refund|compensation|reimbursement)"
    r"|no (?:doubt|question) (?:that )?(?:you|we) will"
    r")\b",
    re.IGNORECASE,
)

_LEGAL_ADVICE = re.compile(
    r"\b(?:"
    r"legal advice|financial advice"
    r"|you should (?:sue|contact a lawyer|take legal action)"
    r"|we recommend (?:suing|litigation)"
    r"|this (?:is|constitutes) (?:legal|financial) advice"
    r")\b",
    re.IGNORECASE,
)

_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "been",
    "before",
    "being",
    "could",
    "dear",
    "does",
    "from",
    "have",
    "here",
    "into",
    "just",
    "kind",
    "letter",
    "more",
    "must",
    "need",
    "only",
    "please",
    "regards",
    "should",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "very",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "your",
}


class AuditDecision(StrEnum):
    """What the Auditor decided."""

    APPROVE = "approve"
    REJECT = "reject"


@dataclass(slots=True)
class AuditIssue:
    """One reason to reject a draft."""

    code: str
    detail: str
    span: str = ""
    value: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "detail": self.detail,
            "span": self.span,
            "value": self.value,
        }


@dataclass(slots=True)
class AuditResult:
    """`{decision, reasons[], uncited_claims[]}` plus structured issues."""

    decision: AuditDecision
    reasons: list[str] = field(default_factory=list)
    uncited_claims: list[str] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.decision is AuditDecision.APPROVE

    @property
    def codes(self) -> list[str]:
        return [issue.code for issue in self.issues]

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "uncited_claims": list(self.uncited_claims),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _sentences(body: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(body) if part.strip()]


def _content_terms(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9-]{3,}", text.lower())
    return {word for word in words if word not in _STOPWORDS}


def _amounts(text: str) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    for match in _AMOUNT.finditer(text):
        try:
            found.append((match.group(0), round(float(match.group(1).replace(",", "")), 2)))
        except ValueError:
            continue
    return found


def _dates(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in _DATE.finditer(text):
        parsed = normalize_date(match.group(0))
        if parsed is not None:
            found.append((match.group(0), parsed.isoformat()))
    return found


def _supported_by_corpus(term_set: set[str], corpus: str) -> bool:
    if not term_set:
        return True
    corpus_terms = _content_terms(corpus)
    # One distinctive matching term is enough for a conservative
    # deterministic check; the point is to catch an invented claim, not
    # to run an entailment model.
    return bool(term_set & corpus_terms)


def _quantity_supported(token: str, corpus: str) -> bool:
    """Whether a percentage or duration appears in the verified corpus."""
    lowered = " ".join(corpus.lower().split())
    candidates = {
        token.lower(),
        token.lower().replace("-", " "),
        token.lower().replace(" ", "-"),
        re.sub(r"[^0-9.]", "", token),
    }
    return any(candidate and candidate in lowered for candidate in candidates)


def _is_negated_promise(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(
        phrase in lowered
        for phrase in (
            "cannot guarantee",
            "can't guarantee",
            "no guarantee",
            "not guaranteed",
            "cannot promise",
            "can't promise",
            "do not promise",
        )
    )


def audit_draft(
    draft: DraftAction,
    *,
    facts: VerifiedFacts,
    source_spans: list[str] | None = None,
    item: HouseholdItem | None = None,
    household_names: list[str] | None = None,
) -> AuditResult:
    """Review a draft against verified facts and source spans.

    Args:
        draft: The draft to review. `body` is the text a human would send.
        facts: The Verifier's output.
        source_spans: Exact spans from the source document. A claim whose
            terms appear in one of them is cited.
        item: The household item, for allowed personal detail.
        household_names: Names the redactor knows. They are allowed only
            when they are the holder's own name.
    """
    issues: list[AuditIssue] = []
    uncited: list[str] = []
    body = draft.body.strip()
    spans = [span for span in (source_spans or []) if span]
    allowed_amounts = set(facts.amounts)
    if item is not None and item.amount is not None:
        allowed_amounts.add(round(float(item.amount), 2))
    allowed_dates = set(facts.dates)
    if item is not None and item.renewal_date is not None:
        allowed_dates.add(item.renewal_date.isoformat())

    corpus = " ".join(facts.facts) + " " + " ".join(spans)
    if item is not None:
        # `kind` is deliberately absent: "insurance" is a category, not
        # evidence that a specific policy term exists.
        corpus += f" {item.provider} {item.holder or ''} {item.reference or ''}"

    if not body:
        issues.append(AuditIssue("empty_draft", "the draft is empty"))

    # 1. Amounts.
    for written, value in _amounts(body):
        if not any(abs(value - allowed) <= CENT for allowed in allowed_amounts):
            issues.append(
                AuditIssue(
                    "amount_mismatch",
                    f"draft states {written}, which is not in the verified facts",
                    span=written,
                    value=f"{value:.2f}",
                )
            )

    # 2. Dates.
    for written, iso in _dates(body):
        if iso not in allowed_dates:
            issues.append(
                AuditIssue(
                    "date_mismatch",
                    f"draft states {written}, which is not the verified date",
                    span=written,
                    value=iso,
                )
            )

    # 3. Uncited factual assertions.
    for sentence in _sentences(body):
        has_period = bool(_PERCENT.search(sentence) or _PERIOD.search(sentence))
        has_marker = bool(_CLAIM_MARKERS.search(sentence))
        if not (has_period or has_marker):
            continue
        # A sentence with a verified amount/date is supported by a fact.
        amounts = [value for _written, value in _amounts(sentence)]
        dates = [iso for _written, iso in _dates(sentence)]
        amounts_ok = all(
            any(abs(value - allowed) <= CENT for allowed in allowed_amounts)
            for value in amounts
        )
        dates_ok = all(iso in allowed_dates for iso in dates)
        if amounts and amounts_ok and not has_period:
            continue
        if dates and dates_ok and not has_period:
            continue
        if _supported_by_corpus(_content_terms(sentence), corpus):
            continue
        uncited.append(sentence)
        issues.append(
            AuditIssue(
                "uncited_claim",
                "factual assertion has no Verifier fact or source span",
                span=sentence,
            )
        )

    # Numbers that look like a percentage or a duration are factual
    # claims even without a marker word.
    for sentence in _sentences(body):
        if sentence in uncited:
            continue
        for match in list(_PERCENT.finditer(sentence)) + list(_PERIOD.finditer(sentence)):
            if not _quantity_supported(match.group(0), corpus):
                uncited.append(sentence)
                issues.append(
                    AuditIssue(
                        "uncited_claim",
                        f"quantity {match.group(0)!r} has no Verifier fact or source span",
                        span=sentence,
                    )
                )
                break

    # 4. Promised outcomes and legal-advice phrasing.
    for sentence in _sentences(body):
        if _PROMISED_OUTCOME.search(sentence) and not _is_negated_promise(sentence):
            issues.append(
                AuditIssue(
                    "promised_outcome",
                    "draft promises an outcome the agent cannot guarantee",
                    span=sentence,
                )
            )
        if _LEGAL_ADVICE.search(sentence):
            issues.append(
                AuditIssue(
                    "legal_advice",
                    "draft uses legal- or financial-advice phrasing",
                    span=sentence,
                )
            )

    # 5. Personal detail beyond what the user's own letter needs.
    allowed_personal = set(draft.personal_details)
    if item is not None:
        allowed_personal.update(
            value
            for value in (item.reference, item.holder, item.provider)
            if value
        )
    if draft.recipient:
        allowed_personal.add(draft.recipient)
    redacted = local_redact(body, names=household_names or [])
    for redaction in redacted.redactions:
        if redaction.original in allowed_personal:
            continue
        issues.append(
            AuditIssue(
                "unexpected_personal_data",
                f"draft carries an unexpected {redaction.category.value} beyond the user's own reference",
                span=redaction.original,
                value=redaction.category.value,
            )
        )

    if issues:
        reasons = [issue.detail for issue in issues]
        return AuditResult(
            decision=AuditDecision.REJECT,
            reasons=reasons,
            uncited_claims=uncited,
            issues=issues,
        )
    return AuditResult(decision=AuditDecision.APPROVE)


def _replace_amounts(body: str, target: float | None) -> str:
    if target is None:
        return body
    formatted = f"${target:,.2f}"

    def substitute(match: re.Match[str]) -> str:
        try:
            value = round(float(match.group(1).replace(",", "")), 2)
        except ValueError:
            return match.group(0)
        return formatted if abs(value - target) > CENT else match.group(0)

    return _AMOUNT.sub(substitute, body)


def _replace_dates(body: str, target: date | None) -> str:
    if target is None:
        return body
    formatted = target.strftime("%d %B %Y")

    def substitute(match: re.Match[str]) -> str:
        parsed = normalize_date(match.group(0))
        return match.group(0) if parsed == target else formatted

    return _DATE.sub(substitute, body)


def _drop_sentences(body: str, sentences: set[str]) -> str:
    """Remove exact sentences, preserving the draft's line structure."""
    if not sentences:
        return body
    for sentence in sentences:
        if sentence:
            body = body.replace(sentence, "")
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _mask_originals(body: str, originals: set[str]) -> str:
    for original in sorted(originals, key=len, reverse=True):
        if original:
            body = body.replace(original, "[redacted]")
    return body


def revise_draft(
    draft: DraftAction,
    audit: AuditResult,
    *,
    facts: VerifiedFacts,
    item: HouseholdItem | None = None,
) -> DraftAction:
    """Apply deterministic repairs for the Auditor's issues.

    The revision is intentionally mechanical: replace a wrong amount or
    date with the verified one; remove a sentence that is uncited, promises
    an outcome, or gives legal advice; mask personal detail that the letter
    does not need.
    """
    body = draft.body
    dropped: set[str] = set()
    correct_amount = facts.stated_total
    if correct_amount is None and item is not None:
        correct_amount = item.amount
    correct_date = None
    if facts.deadline:
        try:
            correct_date = date.fromisoformat(facts.deadline)
        except ValueError:
            correct_date = None

    for issue in audit.issues:
        if issue.code == "amount_mismatch":
            body = _replace_amounts(body, correct_amount)
            if correct_amount is None and issue.span:
                dropped.update(
                    sentence for sentence in _sentences(body) if issue.span in sentence
                )
        elif issue.code == "date_mismatch":
            body = _replace_dates(body, correct_date)
            if correct_date is None and issue.span:
                dropped.update(
                    sentence for sentence in _sentences(body) if issue.span in sentence
                )
        elif issue.code in {"uncited_claim", "promised_outcome", "legal_advice", "empty_draft"}:
            if issue.span:
                dropped.add(issue.span)
        elif issue.code == "unexpected_personal_data" and issue.span:
            body = _mask_originals(body, {issue.span})

    body = _drop_sentences(body, dropped)
    metadata = dict(draft.metadata)
    metadata["revisions"] = int(metadata.get("revisions", 0)) + 1
    metadata["revision_codes"] = audit.codes
    return replace(draft, body=body, metadata=metadata)
