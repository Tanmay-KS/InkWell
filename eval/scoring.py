"""Field-level scoring for the document benchmarks.

Phase 0 — Person B.

Both benchmarks — reading a rendered page, and reading redacted text —
answer the same question: *did we recover the fields the pipeline needs?*
So the scoring lives here and both use it.

The metric is **field recovery**, not string similarity. A model that
transcribes a page beautifully but mangles the account number is not good
enough for us, and an edit-distance metric would not notice: it would
report the prose as near-perfect and the identifier as a small typo. We
know what is in each document — the generator wrote the ground truth —
so we ask directly whether the extracted text contains the renewal date,
the account number, the provider, the holder, and the amount.

Two details that matter:

* **Account numbers appear in several formats** — `1234-5678`, `1234 5678`,
  `12345678`, `POL-482913-Z`. Scoring has to treat them as the same value.
* **Dates get reordered.** OCR of a rendered page can produce `2027-03-07`,
  `03/07/2027`, or `07/03/2027` for the same date. Counting the reorder as
  a miss would punish a model for a formatting difference the pipeline can
  trivially normalise — but counting it as a hit would hide a real problem.
  So the three numeric orderings are all accepted, and the per-field
  breakdown shows which one hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The fields whose recovery actually matters downstream. The date and the
# account number are the two the pipeline cannot work without.
SCORED_FIELDS = [
    "renewal_date",
    "account_number",
    "provider",
    "holder",
    "amount",
]

_MONTHS = {
    1: "january",
    2: "february",
    3: "march",
    4: "april",
    5: "may",
    6: "june",
    7: "july",
    8: "august",
    9: "september",
    10: "october",
    11: "november",
    12: "december",
}


def normalise_alnum(value: str) -> str:
    """Lowercase, and drop everything that is not a letter or digit."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def normalise_numeric(value: str) -> str:
    """Keep digits and decimal points; drop currency symbols and separators."""
    return re.sub(r"[^0-9.]", "", value)


_NUMBER_TOKEN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def numeric_tokens(text: str) -> set[str]:
    """Every number in the text, as a standalone token.

    Stripping everything but digits from a whole document would mash every
    number in it into one long run — `2027-03-07 ... $148.95 ... 0847-4851`
    becomes `202703071489.9508474851` — and then a boundary check can never
    match. The numbers have to be pulled out as tokens before they are
    compared.
    """
    return {match.group(0).replace(",", "") for match in _NUMBER_TOKEN.finditer(text)}


def _digit_match(haystack: str, needle: str) -> bool:
    """Match a digit run without letting it match part of a longer run.

    `haystack` is alnum-normalised, so the digits of `1234-5678` sit
    contiguously as `12345678`. The boundary check stops a six-digit
    account number from matching inside a ten-digit one.
    """
    if not needle:
        return False
    return re.search(rf"(?<![0-9]){re.escape(needle)}(?![0-9])", haystack) is not None


def date_variants(iso_date: str) -> set[str]:
    """Every numeric spelling of an ISO date, normalised to digits.

    `2027-03-07` becomes `20270307`, `03072027`, and `07032027`. A date
    written in words (`March 7, 2027`) is not included — that is a
    different failure and the per-field breakdown should show it.
    """
    try:
        year, month, day = (int(part) for part in iso_date.split("-"))
    except (ValueError, TypeError):
        return set()

    variants = {
        f"{year:04d}{month:02d}{day:02d}",
        f"{day:02d}{month:02d}{year:04d}",
        f"{month:02d}{day:02d}{year:04d}",
    }
    # `07` and `7` are both legitimate readings of the same day.
    variants |= {
        f"{year:04d}{month:02d}{day}",
        f"{day}{month:02d}{year:04d}",
        f"{month:02d}{day}{year:04d}",
    }
    return variants


def date_word_variants(iso_date: str) -> set[str]:
    """Spellings of an ISO date that use month names."""
    try:
        year, month, day = (int(part) for part in iso_date.split("-"))
    except (ValueError, TypeError):
        return set()

    name = _MONTHS.get(month, "")
    if not name:
        return set()
    return {
        f"{name}{day}{year}",
        f"{day}{name}{year}",
        f"{name}{year}{day}",
        f"{year}{name}{day}",
    }


def amount_variants(value: str) -> set[str]:
    """Every acceptable numeric spelling of an amount.

    `$148.95` and `$1,234.56` appear in the corpus, sometimes with
    "per month" appended. The trailing-zero form (`$12.00`) is accepted
    as `$12` too, but only when the shortened form is still specific
    enough to be evidence of a real match.
    """
    variants = set()
    digits = normalise_numeric(value)
    if not digits:
        return variants

    variants.add(digits)
    if digits.endswith(".00"):
        variants.add(digits[:-3])
    if "." in digits:
        variants.add(digits.rstrip("0").rstrip("."))

    # Anything shorter than three characters will match by accident.
    return {v for v in variants if len(v.replace(".", "")) >= 3}


def field_matches(field_name: str, truth_value: str, extracted: str) -> bool:
    """Whether the extracted text contains the truth value for one field."""
    if not truth_value:
        return False

    haystack = normalise_alnum(extracted)
    if not haystack:
        return False

    if field_name == "renewal_date":
        for variant in date_variants(truth_value):
            if _digit_match(haystack, variant):
                return True
        return any(variant in haystack for variant in date_word_variants(truth_value))

    if field_name == "amount":
        tokens = numeric_tokens(extracted)
        return any(variant in tokens for variant in amount_variants(truth_value))

    needle = normalise_alnum(truth_value)
    if not needle:
        return False

    if needle.isdigit():
        return _digit_match(haystack, needle)
    return needle in haystack


@dataclass
class FieldScore:
    """The score for one document."""

    matched: dict[str, bool] = field(default_factory=dict)
    total: int = 0

    @property
    def found(self) -> int:
        return sum(1 for hit in self.matched.values() if hit)

    def as_dict(self) -> dict[str, bool]:
        return dict(self.matched)


def score_document(truth: dict, extracted: str) -> FieldScore:
    """Score one document's extraction against its ground truth.

    Args:
        truth: The ground-truth record from the generator.
        extracted: The text the extractor produced.

    Returns:
        A `FieldScore` with one boolean per scored field.
    """
    score = FieldScore()
    for field_name in SCORED_FIELDS:
        value = truth.get(field_name)
        if not value:
            continue
        score.total += 1
        score.matched[field_name] = field_matches(field_name, str(value), extracted)
    return score


@dataclass
class AggregateScore:
    """The score across a run."""

    docs: int = 0
    per_field_found: dict[str, int] = field(default_factory=dict)
    per_field_total: dict[str, int] = field(default_factory=dict)

    @property
    def fields_found(self) -> int:
        return sum(self.per_field_found.values())

    @property
    def fields_total(self) -> int:
        return sum(self.per_field_total.values())

    @property
    def accuracy(self) -> float:
        if self.fields_total == 0:
            return 0.0
        return self.fields_found / self.fields_total

    def field_rate(self, field_name: str) -> float:
        total = self.per_field_total.get(field_name, 0)
        if total == 0:
            return 0.0
        return self.per_field_found.get(field_name, 0) / total

    def add(self, score: FieldScore) -> None:
        self.docs += 1
        for field_name, hit in score.matched.items():
            self.per_field_found[field_name] = self.per_field_found.get(field_name, 0) + int(hit)
            self.per_field_total[field_name] = self.per_field_total.get(field_name, 0) + 1


def render_field_table(aggregate: AggregateScore, fields: list[str] | None = None) -> str:
    """Render the per-field breakdown as a markdown fragment."""
    fields = fields or SCORED_FIELDS
    lines = ["| Field | Recovered |", "|---|---:|"]
    for field_name in fields:
        total = aggregate.per_field_total.get(field_name, 0)
        if total == 0:
            continue
        found = aggregate.per_field_found.get(field_name, 0)
        lines.append(f"| `{field_name}` | {found}/{total} ({found / total * 100:.1f}%) |")
    return "\n".join(lines)
