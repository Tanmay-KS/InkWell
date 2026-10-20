"""Tests for the field-recovery scoring.

The scorer is the thing that decides whether a model is good enough. If
it is wrong, every number the project reports is wrong in a way that is
hard to notice — which is exactly what happened once already: the `amount`
branch compared the ground truth against itself and therefore counted
every amount as recovered.

The invariant that catches that class of bug is the first test here:
**a document scored against its own text must score 100% on every
field.** The baseline is the only candidate that cannot fail, so if the
baseline is not perfect, the scorer is broken.
"""

from __future__ import annotations

from eval.scoring import (
    SCORED_FIELDS,
    amount_variants,
    date_variants,
    field_matches,
    numeric_tokens,
    score_document,
)


def test_a_document_scored_against_its_own_text_is_perfect():
    """The baseline cannot fail — so if it does, the scorer is broken."""
    truth = {
        "renewal_date": "2027-03-07",
        "account_number": "1234-5678",
        "provider": "Northwind Fitness",
        "holder": "Sam Rivera",
        "amount": "$148.95 per month",
    }
    text = (
        "Northwind Fitness\n"
        "Membership Renewal Notice\n\n"
        "Hello Sam Rivera,\n\n"
        "Your membership runs through 2027-03-07. Your rate is $148.95 per month.\n\n"
        "Member name: Sam Rivera\n"
        "Member number: 1234-5678\n"
    )
    score = score_document(truth, text)
    assert score.total == len(SCORED_FIELDS)
    assert score.found == score.total, score.matched


def test_account_number_formats_are_all_the_same_value():
    """A router or a scorer that only knows one format is not one."""
    for written in ("1234-5678", "1234 5678", "12345678"):
        assert field_matches("account_number", "1234-5678", f"Member number: {written}"), written


def test_alphanumeric_account_numbers_are_matched():
    """`POL-482913-Z` has to be recoverable too."""
    assert field_matches("account_number", "POL-482913-Z", "Policy number: POL-482913-Z")


def test_a_different_account_number_does_not_match():
    assert not field_matches("account_number", "1234-5678", "Member number: 9999-0000")


def test_date_orderings_all_count_as_the_same_date():
    """OCR reorders dates. That is a formatting difference, not a miss."""
    for written in ("2027-03-07", "03/07/2027", "07/03/2027", "March 7, 2027"):
        assert field_matches("renewal_date", "2027-03-07", f"Date of expiry: {written}"), written


def test_date_variants_cover_the_numeric_orders():
    variants = date_variants("2027-03-07")
    assert "20270307" in variants
    assert "03072027" in variants
    assert "07032027" in variants


def test_amount_matches_across_formats():
    for written in ("$148.95", "$148.95 per month", "148.95"):
        assert field_matches("amount", "$148.95", f"Your contribution is {written}."), written


def test_amount_matches_with_a_thousands_separator():
    assert field_matches("amount", "$1,234.56", "Your rate is $1,234.56 per month.")


def test_amount_variants_drop_trailing_zeros():
    """`$12.00` and `$12` are the same amount, but only the exact spelling
    counts — a two-digit variant would match by accident."""
    assert amount_variants("$12.00") == {"12.00"}
    assert amount_variants("$148.95") == {"148.95"}


def test_a_different_amount_does_not_match():
    assert not field_matches("amount", "$148.95", "Your contribution is $999.99.")


def test_numeric_tokens_are_not_mashed_together():
    """The bug that made every amount fail.

    Stripping a whole document of everything but digits turns
    `2027-03-07 ... $148.95 ... 0847-4851` into one long run, and then no
    boundary check can ever match.
    """
    text = "Your plan renews on 2027-03-07. Your rate is $148.95.\nMember ID: 0847-4851"
    tokens = numeric_tokens(text)
    assert "148.95" in tokens
    assert "2027" in tokens
    assert "03" in tokens
    assert "0847" in tokens
    assert "4851" in tokens


def test_a_short_amount_is_not_matched_by_accident():
    """A two-digit amount would match by chance, so it is not a needle."""
    assert field_matches("amount", "$12.00", "Your rate is $12.00.")
    assert not field_matches("amount", "$12.00", "Reference 12345")


def test_holder_names_are_matched():
    assert field_matches("holder", "Sam Rivera", "Policy holder: Sam Rivera")


def test_provider_names_are_matched():
    assert field_matches("provider", "Northwind Fitness", "Northwind Fitness\nRenewal Notice")
