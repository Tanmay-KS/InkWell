"""Tests for the deterministic redaction pass.

The local pass is the part of the privacy router that costs nothing and
never lets a detail out, so it is worth testing properly. The model-assisted
escalation is Phase 2 and is not covered here.
"""

from __future__ import annotations

import itertools

from privacy_router.redact import (
    Category,
    contains_personal_detail,
    find_spans,
    local_redact,
    redact,
)


def test_redacts_email_addresses():
    result = local_redact("Send the receipt to sam.rivera@example.com please.")
    assert "sam.rivera@example.com" not in result.text
    assert "[EMAIL_1]" in result.text
    assert Category.EMAIL in result.categories


def test_redacts_phone_numbers_in_several_formats():
    for phone in [
        "5035550148",
        "503-555-0148",
        "(503) 555-0148",
        "+1 503 555 0148",
    ]:
        result = local_redact(f"Call {phone} to confirm.")
        assert phone not in result.text, phone
        assert Category.PHONE in result.categories, phone


def test_redacts_account_numbers():
    result = local_redact("Your account number is 1234-5678 and the PIN is 4821.")
    assert "1234-5678" not in result.text
    assert "4821" in result.text, "a four-digit PIN is not an account number"


def test_redacts_grouped_account_numbers():
    result = local_redact("Account 1234 5678 9012 3456 was charged.")
    assert "1234 5678 9012 3456" not in result.text
    assert Category.ACCOUNT in result.categories


def test_does_not_redact_a_date_that_is_not_a_birth_date():
    """Masking renewal dates would break the deadline detection."""
    text = "Your policy renews on 03/15/2027."
    result = local_redact(text)
    assert "03/15/2027" in result.text
    assert Category.DOB not in result.categories


def test_redacts_a_date_of_birth_when_the_context_says_so():
    text = "Date of birth: 04/17/1989 is recorded on your file."
    result = local_redact(text)
    assert "04/17/1989" not in result.text
    assert Category.DOB in result.categories


def test_redacts_ibans():
    result = local_redact("Pay to GB82WEST12345698765432 today.")
    assert "GB82WEST12345698765432" not in result.text
    assert Category.IBAN in result.categories


def test_a_long_number_that_fails_luhn_is_an_account_not_a_card():
    """A bare 16-digit identifier that fails Luhn is an account number.

    It is still personal data and is still masked — the label differs, not
    the decision. The Luhn check only decides whether to call it a card.
    """
    result = local_redact("Reference 1234567890123456 was cited.")
    assert "1234567890123456" not in result.text
    assert Category.ACCOUNT in result.categories
    assert Category.CARD not in result.categories


def test_redacts_a_number_that_passes_luhn():
    # 4111 1111 1111 1111 is the standard Luhn-valid test card number.
    result = local_redact("Card 4111 1111 1111 1111 was charged.")
    assert "4111 1111 1111 1111" not in result.text
    assert Category.CARD in result.categories


def test_redacts_postal_addresses():
    result = local_redact("We moved to 4128 Fenwick Street last month.")
    assert "Fenwick Street" not in result.text
    assert Category.ADDRESS in result.categories


def test_the_same_value_keeps_the_same_placeholder():
    text = "Contact sam@example.com or sam@example.com again."
    result = local_redact(text)
    assert result.text.count("[EMAIL_1]") == 2
    assert "[EMAIL_2]" not in result.text


def test_different_values_get_different_placeholders():
    text = "Contact sam@example.com or priya@example.com."
    result = local_redact(text)
    assert "[EMAIL_1]" in result.text
    assert "[EMAIL_2]" in result.text


def test_clean_text_is_unchanged():
    text = "Your membership renews on 15 January 2027. No action is required."
    result = local_redact(text)
    assert result.text == text
    assert result.redactions == []


def test_empty_text_is_handled():
    result = local_redact("")
    assert result.text == ""
    assert result.redactions == []


def test_find_spans_does_not_overlap():
    text = "Call 503-555-0148 or email sam@example.com, account 1234-5678."
    spans = find_spans(text)
    for previous, following in itertools.pairwise(spans):
        assert previous[1] <= following[0], "spans overlap"


def test_contains_personal_detail_reports_categories():
    categories = contains_personal_detail("Email sam@example.com")
    assert Category.EMAIL in categories
    assert contains_personal_detail("Nothing to see here") == set()


def test_redact_without_a_model_is_the_local_pass():
    text = "Email sam@example.com"
    result = redact(text)
    assert "sam@example.com" not in result.text
    assert result.escalated is False


def test_redact_with_a_model_is_not_implemented_yet():
    """The escalation must fail loudly, not silently fall back to the local
    pass — a caller that believes it got semantic redaction and did not
    would understate the leak rate."""
    import pytest

    with pytest.raises(NotImplementedError):
        redact("Email sam@example.com", model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
