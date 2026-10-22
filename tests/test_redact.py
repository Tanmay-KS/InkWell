"""Tests for the redaction pass.

The local pass is the part of the privacy router that costs nothing and
never lets a detail out, so it is worth testing properly. The model-assisted
escalation is the router's one outbound call, so what it *carries* is
tested as carefully as what it removes.
"""

from __future__ import annotations

import itertools

from privacy_router.egress import capture, capturing_transport
from privacy_router.redact import (
    Category,
    contains_personal_detail,
    find_spans,
    local_redact,
    parse_residual_spans,
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


def test_redact_escalates_and_masks_what_the_model_found():
    """The escalation is a real outbound call, and its answer is applied."""
    with capture() as sink:
        result = redact(
            "Account 1234-5678 is under Sarah Whitfield",
            names=["Devin Sullivan"],
            transport=capturing_transport('{"spans": ["Sarah Whitfield"]}'),
        )

    assert result.escalated is True
    assert "Sarah Whitfield" not in result.text
    assert "1234-5678" not in result.text

    [request] = sink.requests
    assert request.kind == "redact_call"
    # The escalation carries the *locally redacted* text: the account is
    # already a placeholder by the time it goes out. That ordering is the
    # whole argument for allowing the call at all.
    assert "1234-5678" not in request.payload
    assert "[ACCOUNT_1]" in request.payload


def test_an_unparseable_escalation_answer_is_not_a_clean_bill_of_health():
    """The parser reports what it found, and nothing when it understood
    nothing — the caller learns which through `escalated`, not through a
    silent success."""
    assert parse_residual_spans("I could not do that") == []
    assert parse_residual_spans("") == []
    assert parse_residual_spans('{"spans": ["[NAME_1]", ""]}') == []
    assert parse_residual_spans('{"spans": ["Sarah"]}') == ["Sarah"]


def test_a_name_does_not_run_past_the_end_of_its_line():
    """The separator between the words of a name is a space, not any
    whitespace. With `\\s+` a name swallowed the first word of the next
    line — "Policy holder: Devin Sullivan" ate the "Member" of
    "Member number:"."""
    result = local_redact("Policy holder: Devin Sullivan\nMember number: SUB-281674")
    assert result.text == "Policy holder: [NAME_1]\nMember number: [ACCOUNT_1]"
    assert "Member" in result.text


def test_the_redaction_map_resolves_placeholders_back():
    """The map is how the agent writes a useful draft without the value
    ever travelling to the model."""
    from privacy_router.redact import redacted_text

    text, mapping = redacted_text("Member number: SUB-281674")
    assert "SUB-281674" not in text
    assert mapping.resolve(text) == "Member number: SUB-281674"


def test_the_redaction_map_does_not_leak_in_its_repr():
    """The map is a trapdoor, and a trapdoor should not print itself."""
    from privacy_router.redact import redacted_text

    _text, mapping = redacted_text("Member number: SUB-281674")
    assert "SUB-281674" not in repr(mapping)


# --- names ------------------------------------------------------------------
#
# The audit found holder names at 0% detected: `Category.NAME` existed but
# had no pattern, so every name in the corpus left the sandbox. A name is
# found in three ways, and each has a test.


def test_redacts_a_name_after_a_label():
    result = local_redact("Policy holder: Devin Sullivan")
    assert "Devin Sullivan" not in result.text
    assert "[NAME_1]" in result.text
    assert Category.NAME in result.categories


def test_the_label_itself_is_not_masked():
    """Masking the label would take the structure of the document with it."""
    result = local_redact("Policy holder: Devin Sullivan")
    assert "Policy holder:" in result.text


def test_redacts_a_name_in_a_salutation():
    result = local_redact("Dear Devin Sullivan,\n\nYour policy renews soon.")
    assert "Devin Sullivan" not in result.text
    assert Category.NAME in result.categories


def test_redacts_a_known_name_anywhere():
    """The household's names are in the memory layer, so the redactor is
    told them rather than having to recognise them in prose."""
    text = "This certifies that Casey Lindqvist holds a registration."
    result = local_redact(text, names=["Casey Lindqvist"])
    assert "Casey Lindqvist" not in result.text
    assert Category.NAME in result.categories


def test_a_known_name_that_is_not_present_changes_nothing():
    result = local_redact("Nothing personal here.", names=["Casey Lindqvist"])
    assert result.text == "Nothing personal here."
    assert result.redactions == []


def test_a_longer_known_name_is_masked_as_one_span():
    """`Alex Chen` must not be masked as `Alex` and `Chen` separately."""
    result = local_redact("Alex Chen called.", names=["Alex Chen", "Alex"])
    assert "[NAME_1]" in result.text
    assert result.text.count("[NAME_1]") == 1


# --- addresses --------------------------------------------------------------


def test_redacts_the_city_and_the_postcode_too():
    """Masking only the street left enough to identify someone."""
    result = local_redact("Registered address: 3482 Thistlewood Place, Marrow, CO 80211")
    for value in ("Marrow", "80211", "Thistlewood"):
        assert value not in result.text, value
    assert Category.ADDRESS in result.categories


def test_redacts_a_place_with_no_street_number():
    result = local_redact("Our office is at Marrow, CO 80211 now.")
    assert "Marrow" not in result.text
    assert "80211" not in result.text


def test_a_salutation_is_not_read_as_a_city():
    """`Dear Sam, CA 90210` is not a city called "Dear Sam".

    The salutation word is structure, and structure has to survive
    redaction — the reader still needs to know the letter is addressed
    to someone.
    """
    result = local_redact("Dear Sam, CA 90210")
    assert "Dear" in result.text
    assert "90210" not in result.text
    assert "Sam" not in result.text


# --- reference codes --------------------------------------------------------


def test_redacts_an_alphanumeric_reference_code():
    """`POL-4825232-I` matched neither account pattern and used to survive."""
    result = local_redact("Policy number: POL-482913-Z")
    assert "POL-482913-Z" not in result.text
    assert Category.ACCOUNT in result.categories


def test_redacts_an_alphanumeric_account_number():
    result = local_redact("Account: SUB-281674")
    assert "SUB-281674" not in result.text


def test_does_not_redact_a_standards_reference():
    """Four digits is not enough to be an identifier — `ISO-8601` is a
    standard, not a policy number."""
    result = local_redact("The standard is ISO-8601.")
    assert "ISO-8601" in result.text


# --- label-driven categories ------------------------------------------------


def test_a_ten_digit_account_number_is_not_reported_as_a_phone():
    """Ten digits are a phone number or an account number. Only the label
    can say which, and the leak test scores by category."""
    result = local_redact("Account: 9051054692")
    assert "9051054692" not in result.text
    assert Category.ACCOUNT in result.categories
    assert Category.PHONE not in result.categories


def test_a_labelled_phone_number_is_still_a_phone():
    result = local_redact("Phone: 9051054692")
    assert Category.PHONE in result.categories


def test_a_labelled_email_is_still_an_email():
    result = local_redact("Email: sam@example.com")
    assert Category.EMAIL in result.categories


def test_a_label_further_away_does_not_reclassify():
    """`Your account number is 1234-5678` is not a labelled field, and
    guessing at a label two words away is how a redactor starts eating
    the document."""
    result = local_redact("Your account number is 1234-5678.")
    assert "1234-5678" not in result.text
    assert Category.ACCOUNT in result.categories
    assert Category.PHONE not in result.categories


# --- things that must not be masked -----------------------------------------


def test_a_provider_name_survives():
    """The provider is not personal, and the agent has to be able to say
    which company the notice is from."""
    result = local_redact("Northwind Fitness\nRenewal Notice")
    assert "Northwind Fitness" in result.text


def test_a_renewal_date_survives():
    """Masking the renewal date would break the deadline detection."""
    result = local_redact("Your policy renews on 2027-03-07.")
    assert "2027-03-07" in result.text


def test_an_amount_survives():
    result = local_redact("Your rate is $148.95 per month.")
    assert "$148.95" in result.text
