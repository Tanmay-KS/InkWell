"""Verifier: facts computed by code, never guessed by a model.

Upgrade Phase C — the Auditor's ground truth.

A draft can say "$184.95" when the notice says "$148.95", and a language
model reviewer can be persuaded by a confident sentence. The Verifier is
not persuadable: it is arithmetic and date subtraction over extracted
fields. Its output is what the Auditor compares against, and the trace
shows it as a `code` step with no egress.

Checks implemented here:

* line items sum to the stated total (with a one-cent rounding tolerance)
* duplicate charges — same vendor and amount with nearby dates, both
  inside one document and across the household inventory
* days remaining to a deadline or refund window
* upcoming expiry inside a configurable window (14 days by default)

Every emitted fact carries a short human-readable sentence as well as the
machine value, because the Auditor's citation check needs something to
cite and the UI needs something to show.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agent.deadlines.flag_deadlines import Deadline, normalize_date
from agent.memory.hermes_memory import HouseholdItem

__all__ = [
    "DEFAULT_DUPLICATE_WINDOW_DAYS",
    "DEFAULT_EXPIRY_WINDOW_DAYS",
    "VerifiedFacts",
    "days_remaining",
    "find_duplicate_charges",
    "inventory_findings",
    "verify",
    "verify_item",
]

DEFAULT_EXPIRY_WINDOW_DAYS = 14
DEFAULT_DUPLICATE_WINDOW_DAYS = 7
CENT = 0.01


def _money(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return normalize_date(text)


def days_remaining(deadline: object, *, today: date | None = None) -> int | None:
    """Calendar days from `today` to a date or deadline.

    Negative means the date has passed. The caller decides whether that
    is a finding; the Verifier does not hide it.
    """
    when = deadline.date if isinstance(deadline, Deadline) else _as_date(deadline)
    if when is None:
        return None
    today = today or date.today()
    return (when - today).days


@dataclass(slots=True)
class VerifiedFacts:
    """Facts the Auditor may cite.

    Attributes:
        vendor: The provider the document is from.
        line_items: `{"label": str, "amount": float}` entries.
        computed_total: Sum of the line items, when there are any.
        stated_total: The total the document states.
        totals_match: True/False, or None when there is nothing to check.
        duplicates: Duplicate-charge findings, each with its evidence.
        deadline: The ISO date of the primary obligation.
        days_remaining: Calendar days from `today` to `deadline`.
        expiring_soon: True when the deadline is inside the expiry window.
        facts: Short, citable sentences.
        sources: Machine values keyed by name, for the Auditor.
    """

    vendor: str | None = None
    line_items: list[dict[str, object]] = field(default_factory=list)
    computed_total: float | None = None
    stated_total: float | None = None
    totals_match: bool | None = None
    duplicates: list[dict[str, object]] = field(default_factory=list)
    deadline: str | None = None
    days_remaining: int | None = None
    expiring_soon: bool = False
    facts: list[str] = field(default_factory=list)
    sources: dict[str, object] = field(default_factory=dict)

    @property
    def amounts(self) -> set[float]:
        """Every amount a draft is allowed to state."""
        values: set[float] = set()
        for value in (self.stated_total, self.computed_total):
            if value is not None:
                values.add(round(value, 2))
        for item in self.line_items:
            amount = _money(item.get("amount"))
            if amount is not None:
                values.add(amount)
        return values

    @property
    def dates(self) -> set[str]:
        """Every date a draft is allowed to state, as ISO strings."""
        return {self.deadline} if self.deadline else set()

    def to_dict(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "line_items": self.line_items,
            "computed_total": self.computed_total,
            "stated_total": self.stated_total,
            "totals_match": self.totals_match,
            "duplicates": self.duplicates,
            "deadline": self.deadline,
            "days_remaining": self.days_remaining,
            "expiring_soon": self.expiring_soon,
            "facts": self.facts,
            "sources": self.sources,
        }


def find_duplicate_charges(
    entries: list[Mapping[str, Any]],
    *,
    window_days: int = DEFAULT_DUPLICATE_WINDOW_DAYS,
    amount_tolerance: float = CENT,
) -> list[dict[str, object]]:
    """Charges with the same vendor and amount and nearby dates.

    Each entry needs `provider`, `amount`, and optionally `date` and
    `item_id`/`label`. Entries with no amount are ignored: an unknown
    charge is not evidence of a duplicate.

    Two charges on the same day are always a duplicate candidate; the
    window is inclusive of the day difference.
    """
    grouped: dict[tuple[str, float], list[Mapping[str, Any]]] = {}
    for entry in entries:
        amount = _money(entry.get("amount"))
        provider = str(entry.get("provider") or entry.get("vendor") or "").strip()
        if amount is None or not provider:
            continue
        grouped.setdefault((provider.lower(), amount), []).append(entry)

    findings: list[dict[str, object]] = []
    for (provider, amount), group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                first_date = _as_date(first.get("date") or first.get("renewal_date"))
                second_date = _as_date(second.get("date") or second.get("renewal_date"))
                if first_date and second_date:
                    apart = abs((second_date - first_date).days)
                    if apart > window_days:
                        continue
                else:
                    apart = None
                findings.append(
                    {
                        "provider": first.get("provider") or first.get("vendor") or provider,
                        "amount": amount,
                        "days_apart": apart,
                        "first": _duplicate_side(first),
                        "second": _duplicate_side(second),
                        "reason": (
                            "same vendor and amount"
                            + (f", {apart} day(s) apart" if apart is not None else "")
                        ),
                    }
                )
    return findings


def _duplicate_side(entry: Mapping[str, Any]) -> dict[str, object]:
    return {
        "item_id": entry.get("item_id"),
        "label": entry.get("label") or entry.get("provider") or entry.get("vendor"),
        "date": str(entry.get("date") or entry.get("renewal_date") or "") or None,
        "amount": _money(entry.get("amount")),
    }


def verify(
    fields: Mapping[str, Any],
    *,
    today: date | None = None,
    expiry_window_days: int = DEFAULT_EXPIRY_WINDOW_DAYS,
    duplicate_window_days: int = DEFAULT_DUPLICATE_WINDOW_DAYS,
    inventory: list[Mapping[str, Any]] | None = None,
) -> VerifiedFacts:
    """Compute the facts a draft is allowed to rely on.

    Args:
        fields: `vendor`, optional `line_items`, `stated_total` (or
            `amount`), `deadline` (or `date`), and optional `charges` to
            check for duplicates.
        today: Reference date for days-remaining.
        expiry_window_days: How close a deadline has to be to count as an
            upcoming expiry.
        duplicate_window_days: Maximum date gap for duplicate detection.
        inventory: Other charges to compare against, so a duplicate in a
            single notice is not missed.
    """
    today = today or date.today()
    vendor = str(fields.get("vendor") or "").strip() or None

    line_items: list[dict[str, object]] = []
    for raw in fields.get("line_items") or []:
        if isinstance(raw, Mapping):
            amount = _money(raw.get("amount"))
            if amount is None:
                continue
            line_items.append(
                {
                    "label": str(raw.get("label") or raw.get("description") or "line item"),
                    "amount": amount,
                }
            )
        else:
            amount = _money(raw)
            if amount is not None:
                line_items.append({"label": "line item", "amount": amount})

    computed_total = (
        round(sum(float(item["amount"]) for item in line_items), 2) if line_items else None
    )
    stated_total = _money(
        fields.get("stated_total", fields.get("total", fields.get("amount")))
    )

    totals_match: bool | None = None
    if computed_total is not None and stated_total is not None:
        totals_match = abs(computed_total - stated_total) <= CENT

    deadline_raw = fields.get("deadline", fields.get("date"))
    deadline_date = _as_date(deadline_raw)
    deadline_iso = deadline_date.isoformat() if deadline_date else None
    remaining = (deadline_date - today).days if deadline_date else None
    expiring_soon = remaining is not None and 0 <= remaining <= expiry_window_days

    charges: list[Mapping[str, Any]] = []
    for raw in fields.get("charges") or []:
        if isinstance(raw, Mapping):
            charges.append(raw)
    if inventory:
        charges.extend(inventory)
    duplicates = find_duplicate_charges(
        charges, window_days=duplicate_window_days
    )

    facts: list[str] = []
    if vendor:
        facts.append(f"Vendor: {vendor}.")
    if totals_match is True:
        facts.append(
            f"Line items sum to {computed_total:.2f}, matching the stated total "
            f"of {stated_total:.2f}."
        )
    elif totals_match is False:
        facts.append(
            f"Line items sum to {computed_total:.2f}, which does not match the "
            f"stated total of {stated_total:.2f}."
        )
    elif stated_total is not None:
        facts.append(f"Stated total: {stated_total:.2f}.")
    if deadline_iso and remaining is not None:
        facts.append(f"Deadline {deadline_iso} is {remaining} day(s) away.")
    if expiring_soon:
        facts.append(f"The deadline is within {expiry_window_days} days.")
    for duplicate in duplicates:
        facts.append(
            f"Possible duplicate charge from {duplicate['provider']} for "
            f"{float(duplicate['amount']):.2f} ({duplicate['reason']})."
        )

    return VerifiedFacts(
        vendor=vendor,
        line_items=line_items,
        computed_total=computed_total,
        stated_total=stated_total,
        totals_match=totals_match,
        duplicates=duplicates,
        deadline=deadline_iso,
        days_remaining=remaining,
        expiring_soon=expiring_soon,
        facts=facts,
        sources={
            "vendor": vendor,
            "stated_total": stated_total,
            "computed_total": computed_total,
            "deadline": deadline_iso,
            "days_remaining": remaining,
            "duplicates": duplicates,
        },
    )


def verify_item(
    item: HouseholdItem,
    *,
    deadline: Deadline | date | str | None = None,
    today: date | None = None,
    inventory: list[Mapping[str, Any]] | None = None,
) -> VerifiedFacts:
    """Verify one household item, using the memory record as the fields."""
    when = deadline
    if when is None:
        when = item.renewal_date
    fields: dict[str, Any] = {
        "vendor": item.provider,
        "amount": item.amount,
        "deadline": when.isoformat() if isinstance(when, date) else when,
        "charges": [
            {
                "provider": item.provider,
                "amount": item.amount,
                "date": item.renewal_date,
                "item_id": item.item_id,
                "label": item.kind,
            }
        ],
    }
    return verify(fields, today=today, inventory=inventory)


def inventory_findings(
    items: list[HouseholdItem],
    *,
    today: date | None = None,
    horizon_days: int = 7,
    expiry_window_days: int = DEFAULT_EXPIRY_WINDOW_DAYS,
    duplicate_window_days: int = DEFAULT_DUPLICATE_WINDOW_DAYS,
) -> dict[str, object]:
    """The Watcher's summary: what is due and what looks duplicated.

    This is the proactive payload. The UI opens on it; chat is secondary.
    """
    today = today or date.today()
    entries = [
        {
            "provider": item.provider,
            "amount": item.amount,
            "date": item.renewal_date,
            "item_id": item.item_id,
            "label": item.kind,
        }
        for item in items
    ]
    duplicates = find_duplicate_charges(entries, window_days=duplicate_window_days)

    due: list[dict[str, object]] = []
    expiring: list[dict[str, object]] = []
    for item in items:
        remaining = days_remaining(item.renewal_date, today=today)
        if remaining is None:
            continue
        row = {
            "item_id": item.item_id,
            "provider": item.provider,
            "kind": item.kind,
            "renewal_date": item.renewal_date.isoformat() if item.renewal_date else None,
            "days_remaining": remaining,
        }
        if 0 <= remaining <= horizon_days:
            due.append(row)
        if 0 <= remaining <= expiry_window_days:
            expiring.append(row)

    findings: list[dict[str, object]] = []
    if due:
        findings.append(
            {
                "kind": "deadline",
                "severity": "warn",
                "title": f"{len(due)} deadline(s) this week",
                "detail": ", ".join(
                    f"{row['provider']} in {row['days_remaining']} day(s)" for row in due
                ),
            }
        )
    if duplicates:
        first = duplicates[0]
        findings.append(
            {
                "kind": "duplicate_charge",
                "severity": "bad",
                "title": f"{len(duplicates)} likely duplicate charge(s)",
                "detail": (
                    f"{first['provider']} for {float(first['amount']):.2f} — "
                    f"{first['reason']}"
                ),
            }
        )
    if not findings:
        findings.append(
            {
                "kind": "clear",
                "severity": "ok",
                "title": "Nothing needs attention this week",
                "detail": "No near deadlines and no duplicate charges in the inventory.",
            }
        )

    summary = "; ".join(str(item["title"]) for item in findings)
    return {
        "generated_for": today.isoformat(),
        "horizon_days": horizon_days,
        "due": due,
        "expiring_soon": expiring,
        "duplicates": duplicates,
        "findings": findings,
        "summary": summary,
    }
