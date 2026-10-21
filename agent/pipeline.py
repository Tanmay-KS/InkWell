"""The pipeline: document in, deadline flagged, draft action out.

Phase 1 — Person B.

This is the plan's Phase 1 exit condition, as code:

    document ──► ingestion ──► memory ──► deadlines ──► action

    ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
    │  bytes   │──►│  text    │──►│ household│──►│ deadline │──►│  draft   │
    │          │   │ (redact) │   │  item    │   │          │   │  action  │
    └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘

Every step runs inside the sandbox, and the order matters. The document
is read locally and redacted locally *before* anything is reasoned about,
so the model — if one is used at all — only ever sees redacted text. The
real values never leave; they are used to write a draft that the user
sees in their own channel.

The whole pipeline runs **without a model call**. That is not a
fallback; it is the design. A renewal notice that says "your policy
renews on 2027-03-07" does not need a language model, and a pipeline
that does not need one cannot leak anything to one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from agent.actions.draft_actions import ActionKind, DraftAction, draft_action
from agent.deadlines.flag_deadlines import Deadline, flag_deadlines
from agent.ingestion.parse_documents import Document, parse_document
from agent.memory.hermes_memory import HouseholdItem, HouseholdMemory, TrackedState

# The organisation a notice is from. Two shapes, in order of confidence:
# "…registration with the City Business Licensing Office" and the
# letterhead on the first line.
_PROVIDER_WITH = re.compile(
    r"\bwith (?:the )?([A-Z][A-Za-z&.'\-]*(?: [A-Z][A-Za-z&.'\-]*){0,4})"
)
_PROVIDER_LETTERHEAD = re.compile(
    r"^([A-Z][A-Za-z&.'\-]*(?:[ ,]*[A-Z&][A-Za-z&.'\-]*){0,4})$"
)

# A line that is a document heading rather than the organisation's name.
_HEADING_WORDS = {
    "renewal notice",
    "official notice of expiry",
    "plan renewal summary",
    "membership renewal notice",
    "subscription update",
    "service notice",
    "notice of expiry",
    "renewal reminder",
}

_AMOUNT = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


@dataclass(slots=True)
class PipelineResult:
    """Everything the pipeline produced for one document.

    Attributes:
        document: The parsed, redacted document.
        item: The household item the document describes.
        deadlines: Every dated obligation found, soonest first.
        action: The drafted action for the primary deadline.
        warnings: Anything that went wrong along the way.
    """

    document: Document
    item: HouseholdItem | None = None
    deadlines: list[Deadline] = field(default_factory=list)
    action: DraftAction | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the pipeline got as far as a usable draft."""
        return self.action is not None and self.item is not None


def guess_provider(text: str) -> str:
    """The organisation a notice is from.

    A letterhead is the first line of the document — but a licence
    notice opens with "OFFICE OF RECORD KEEPING" and names the actual
    office further in, so the "with the …" shape is tried first.

    The search runs on flattened text, because an OCR engine wraps a
    long name across two lines and the name is one name.
    """
    flat = re.sub(r"\s+", " ", text)
    for match in _PROVIDER_WITH.finditer(flat):
        candidate = match.group(1).strip()
        if candidate.lower() not in _HEADING_WORDS:
            return candidate

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() in _HEADING_WORDS:
            continue
        if stripped.isupper():
            # A heading in capitals, like "OFFICE OF RECORD KEEPING".
            continue
        if stripped.endswith(":"):
            continue
        if stripped.lower().startswith(("dear ", "hello ", "hi ", "re: ", "fw: ")):
            continue
        match = _PROVIDER_LETTERHEAD.match(stripped)
        if match:
            return match.group(1).strip()

    return "the provider"


def guess_amount(text: str) -> float | None:
    """The amount mentioned in the document, if there is one."""
    match = _AMOUNT.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def pick_reference(document: Document) -> str | None:
    """The identifier to quote for an obligation.

    A notice often carries more than one: an insurance renewal has both
    a policy number and an account number, and they are not
    interchangeable — the policy number is what the letter is about.

    The label in front of the identifier says which is which, and the
    label is still there in the redacted text, next to the placeholder.
    """
    accounts = document.redactions.by_category(_account_category())
    if not accounts:
        return None

    by_placeholder = {
        placeholder: original
        for placeholder, original in document.redactions.values.items()
        if original in accounts
    }
    if not by_placeholder:
        return accounts[0]

    # Prefer the most specific identifier, judged by the label in front
    # of it.
    priority = ("policy", "registration", "membership", "member", "account")
    best: tuple[int, str] | None = None

    for placeholder, original in by_placeholder.items():
        index = document.text.find(placeholder)
        if index == -1:
            continue
        window = document.text[max(0, index - 40) : index].lower()
        for rank, needle in enumerate(priority):
            if needle in window:
                if best is None or rank < best[0]:
                    best = (rank, original)
                break

    if best is not None:
        return best[1]
    return accounts[0]


def build_item(
    document: Document,
    deadline: Deadline | None,
    *,
    kind: str = "",
    source: str | None = None,
) -> HouseholdItem:
    """Build the household item a document describes.

    The real values come from the redaction map — they are in the
    sandbox, and this is the one place they are needed in the clear.
    """
    provider = guess_provider(document.text)
    holder = next(iter(document.redactions.names()), None)
    reference = pick_reference(document)

    return HouseholdItem(
        item_id=HouseholdItem.make_id(provider, holder, reference),
        kind=kind or guess_kind(document.text),
        provider=provider,
        holder=holder,
        reference=reference,
        renewal_date=deadline.date if deadline else None,
        amount=guess_amount(document.text),
        state=TrackedState.NEW,
        source=source or str(document.source_path),
    )


def _account_category():
    from privacy_router.redact import Category

    return Category.ACCOUNT


def guess_kind(text: str) -> str:
    """What sort of thing the document is about.

    The specific markers come first. `renews on` is in every template,
    so a document that matched on it would always come out as a
    subscription — including the medical and licence ones.
    """
    lowered = text.lower()
    for needle, kind in (
        ("member id", "medical"),
        ("coverage", "medical"),
        ("member services", "medical"),
        ("registration number", "licence"),
        ("registration with the", "licence"),
        ("date of expiry", "licence"),
        ("member number", "membership"),
        ("membership", "membership"),
        ("policy holder", "insurance"),
        ("premium", "insurance"),
        ("insured", "insurance"),
        ("tariff", "utility"),
        ("service address", "utility"),
        ("service notice", "utility"),
        ("subscription", "subscription"),
        ("account holder", "subscription"),
    ):
        if needle in lowered:
            return kind
    return "subscription"


def pick_deadline(deadlines: list[Deadline], *, today: date | None = None) -> Deadline | None:
    """The deadline the user should act on first.

    A future date beats a past one: a document that arrived late still
    has a live obligation in it more often than it has a dead one, and
    the past one is still reported — just not acted on first.
    """
    if not deadlines:
        return None

    future = [d for d in deadlines if not d.is_past]
    if future:
        return future[0]
    return deadlines[0]


def process_document(
    path: str | Path,
    *,
    memory: HouseholdMemory | None = None,
    today: date | None = None,
    kind: str = "",
    action_kind: ActionKind | None = None,
) -> PipelineResult:
    """Run the whole pipeline over one document.

    Args:
        path: The document to read.
        memory: The household memory. When given, the item is stored and
            the household's names are used to redact.
        today: The reference date.
        kind: Force the kind of obligation. Inferred from the text when
            not given.
        action_kind: Force the kind of action. Inferred from the deadline
            when not given.

    Returns:
        A `PipelineResult`.
    """
    today = today or date.today()
    result = PipelineResult(document=Document(source_path=Path(path), text=""))

    # The household's names come from the memory, so the redactor is
    # told them rather than having to recognise them in prose.
    names: list[str] = []
    if memory is not None:
        try:
            names = memory.household_names()
        except Exception as exc:
            result.warnings.append(f"could not read the household picture: {exc}")

    document = parse_document(path, household_names=names)
    result.document = document
    result.warnings.extend(document.warnings)

    if document.is_empty:
        result.warnings.append("no text extracted — nothing to flag")
        return result

    result.deadlines = flag_deadlines(document.text, today=today)
    if not result.deadlines:
        result.warnings.append("no dated obligation found in the document")
        return result

    primary = pick_deadline(result.deadlines, today=today)
    result.item = build_item(document, primary, kind=kind, source=str(path))

    if memory is not None:
        try:
            memory.upsert(result.item)
        except Exception as exc:
            result.warnings.append(f"could not store the item: {exc}")

    result.action = draft_action(primary, context=result.item, kind=action_kind)
    return result
