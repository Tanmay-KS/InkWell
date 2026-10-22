"""Repairing the text an OCR engine hands back.

Phase 1 — Person B.

An OCR engine does not return the text that was on the page. It returns
the text it *guessed*, and the guesses have shapes:

* the space after a colon or a comma is dropped — `Accountnumber:`
  becomes `Accountnumber:` with no space, or `Address:1003Braddock
  Boulevard,Cedar Crossing` with none at all;
* a word that is two words run together keeps its capitals —
  `1003BraddockBoulevard`;
* a lowercase run loses a space — `Dateof expiry:`;
* a character is misread — `Office` comes back as `0ffice`.

The first three are repairable, and repairing them is worth doing:
a redactor and a deadline detector that cannot find `Account number:`
because the OCR dropped a space is a redactor and a detector that do not
work on scanned documents at all.

The fourth is not repairable in general, and nothing here pretends
otherwise. A wrong character is a wrong character, and the thing that
catches it is the model-assisted extraction, not a regex.

**This is a best-effort layer, not a parser.** It repairs the damage
that has a shape; it does not try to understand the document. And it runs
on the *redacted* text, so a repair can never re-introduce a personal
detail that the redactor removed.
"""

from __future__ import annotations

import re

# A space is missing after a colon or a comma when the next character is
# a letter or a digit with no space between them.
#
# The guards matter. `1,234.56` is a thousands separator and `12:30` is a
# time, and inserting a space into either would corrupt a number — which
# is the one thing a document must get exactly right.
_MISSING_SPACE_AFTER_COLON = re.compile(r"(?<![\d:]):(?=[A-Za-z0-9])")
_MISSING_SPACE_AFTER_COMMA = re.compile(r"(?<![0-9]),(?=[A-Za-z])")

# An ampersand between two letters is a word boundary: `Power&Light` is
# `Power & Light`.
_AMPERSAND = re.compile(r"(?<=[A-Za-z])&(?=[A-Za-z])")

# A word boundary inside a run-together word: a lowercase letter or a
# digit followed by a capital. `1003BraddockBoulevard` and `Accountnumber`
# are the same damage with different shapes.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Lowercase words that have run together and are safe to split at a
# known boundary. These are document vocabulary, not general English:
# `Dateof` and `Registeredaddress` are damage, `of` and `address` are
# just words.
_RUN_TOGETHER = [
    (re.compile(r"\bDateof\b", re.IGNORECASE), "Date of"),
    (re.compile(r"\bDateissued\b", re.IGNORECASE), "Date issued"),
    (re.compile(r"\bServiceaddress\b", re.IGNORECASE), "Service address"),
    (re.compile(r"\bRegisteredaddress\b", re.IGNORECASE), "Registered address"),
    (re.compile(r"\bBillingaddress\b", re.IGNORECASE), "Billing address"),
    (re.compile(r"\bHomeaddress\b", re.IGNORECASE), "Home address"),
    (re.compile(r"\bInsuredaddress\b", re.IGNORECASE), "Insured address"),
    (re.compile(r"\bAccountnumber\b", re.IGNORECASE), "Account number"),
    (re.compile(r"\bMembernumber\b", re.IGNORECASE), "Member number"),
    (re.compile(r"\bRegistrationnumber\b", re.IGNORECASE), "Registration number"),
    (re.compile(r"\bPolicynumber\b", re.IGNORECASE), "Policy number"),
    (re.compile(r"\bRenewaldate\b", re.IGNORECASE), "Renewal date"),
    (re.compile(r"\bAccountbalance\b", re.IGNORECASE), "Account balance"),
    (re.compile(r"\bPaymentdue\b", re.IGNORECASE), "Payment due"),
]

# The labels a document is likely to use, in their correctly spaced form.
# Used to check whether a repair worked, and to give the label-driven
# redactor a second chance at a label the OCR mangled.
KNOWN_LABELS = [
    "account number",
    "member number",
    "registration number",
    "policy number",
    "account holder",
    "policy holder",
    "member name",
    "registered holder",
    "registered address",
    "service address",
    "home address",
    "insured address",
    "billing address",
    "billing contact",
    "member services",
    "date of issue",
    "date of expiry",
    "date of birth",
    "renewal date",
    "account balance",
    "payment due",
    "contact details",
]


def _split_camel(token: str) -> str:
    """Split a run-together word at its capitals, when that is safe.

    The guard matters. `AcC-427352` is an account number with a capital
    in the middle of it, and splitting it would turn one identifier into
    two — so a part that contains a digit is left alone.
    """
    if len(token) < 8:
        return token

    parts = _CAMEL.split(token)
    if len(parts) == 1:
        return token

    # A part containing a digit is a continuation, not a new word.
    if any(any(char.isdigit() for char in part) for part in parts[1:]):
        return token

    return " ".join(parts)


def repair_ocr_text(text: str) -> str:
    """Repair the spacing damage an OCR engine leaves behind.

    Args:
        text: The text as the OCR engine returned it.

    Returns:
        The repaired text. Nothing is removed — a repair can only add
        spaces and split words that were joined, so it can never make a
        document *less* readable, and it can never invent a personal
        detail that was not there.
    """
    if not text:
        return text

    repaired = text

    for pattern, replacement in _RUN_TOGETHER:
        repaired = pattern.sub(replacement, repaired)

    # A space after a colon or a comma, but not inside a number or a
    # date. `2027-02-26` has no colon; `12:30` is a time; `1,234.56` is
    # a thousands separator.
    repaired = _MISSING_SPACE_AFTER_COLON.sub(": ", repaired)
    repaired = _MISSING_SPACE_AFTER_COMMA.sub(", ", repaired)
    repaired = _AMPERSAND.sub(" & ", repaired)

    # Split run-together words, per whitespace-delimited token, so a
    # repair cannot cross a line or a field boundary.
    trailing_newline = repaired.endswith("\n")
    lines = []
    for line in repaired.splitlines():
        lines.append(" ".join(_split_camel(token) for token in line.split(" ")))
    repaired = "\n".join(lines)

    # Collapse the double spaces the repairs can leave behind, but keep
    # the line structure.
    repaired = re.sub(r"[ \t]{2,}", " ", repaired)

    # `splitlines` drops the final newline, and a document that ends
    # with one is not a document that has been repaired. Keeping it also
    # means the repair is a no-op on clean text, which is what lets the
    # caller say "nothing was changed" honestly.
    if trailing_newline:
        repaired += "\n"

    return repaired


def label_variants() -> list[str]:
    """The known labels, for a caller that wants to search for them."""
    return list(KNOWN_LABELS)
