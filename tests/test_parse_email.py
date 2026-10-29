"""Tests for the email parser.

The quoted chain is the important part. It is the single largest source
of accidental leakage in email ingestion, because a renewal notice's
reply chain can carry an account number, a support agent's name and a
phone number that the original message never had — and the chain is
stripped before the text reaches any model.
"""

from __future__ import annotations

from datetime import date

from agent.ingestion.parse_email import parse_email, strip_quoted_chain
from agent.pipeline import process_document

PLAIN_EMAIL = """From: Northwind Fitness <billing@northwind.example.com>
To: sam.rivera@example.com
Subject: Your membership renews on 15 January 2027
Message-ID: <abc123@northwind.example.com>
Date: Mon, 20 Oct 2026 09:15:00 +0000
Content-Type: text/plain; charset="utf-8"

Dear Sam Rivera,

Your membership runs through 2027-01-15. Your rate is $42.00 per month.

Member name: Sam Rivera
Member number: 1234-5678

Kind regards,
Northwind Fitness
"""

REPLY_EMAIL = """From: Support <support@northwind.example.com>
To: sam.rivera@example.com
Subject: Re: Your membership renews on 15 January 2027
Message-ID: <def456@northwind.example.com>
Content-Type: text/plain; charset="utf-8"

Thanks for getting in touch — I have checked your account.

On Mon, 20 Oct 2026, Sam Rivera wrote:
> Your membership runs through 2027-01-15.
> Member number: 1234-5678
> Call me on 555-0148 if you need anything.

--
Northwind Fitness Support
"""


def test_strips_a_quoted_chain():
    body = strip_quoted_chain(
        "Thanks for getting in touch.\n\nOn Mon, 20 Oct 2026, Sam Rivera wrote:\n"
        "> Your membership runs through 2027-01-15.\n"
        "> Member number: 1234-5678\n"
    )
    assert "1234-5678" not in body
    assert "wrote:" not in body
    assert "Thanks for getting in touch." in body


def test_strips_a_signature_block():
    """A signature is the sender's own, and it often carries a phone number."""
    body = strip_quoted_chain("The renewal date is 2027-01-15.\n\n--\nSam Rivera\n555-0148\n")
    assert "555-0148" not in body
    assert "2027-01-15" in body


def test_strips_a_gmail_style_chain():
    body = strip_quoted_chain(
        "Your renewal date is 2027-01-15.\n\n"
        "---------- Forwarded message ---------\n"
        "From: someone@example.com\n"
        "Subject: Something else\n"
    )
    assert "someone@example.com" not in body
    assert "2027-01-15" in body


def test_leaves_a_clean_body_alone():
    body = "Your membership runs through 2027-01-15.\n\nKind regards,\nNorthwind Fitness"
    assert strip_quoted_chain(body) == body


def test_parses_a_plain_email():
    parsed = parse_email(PLAIN_EMAIL.encode("utf-8"))
    assert parsed.subject == "Your membership renews on 15 January 2027"
    assert "billing@northwind.example.com" in parsed.sender
    assert parsed.recipients == ["sam.rivera@example.com"]
    assert "2027-01-15" in parsed.body
    assert parsed.message_id == "<abc123@northwind.example.com>"
    assert not parsed.has_attachments


def test_a_reply_loses_its_quoted_chain():
    parsed = parse_email(REPLY_EMAIL.encode("utf-8"))
    assert "1234-5678" not in parsed.body, "the quoted chain must not survive"
    assert "555-0148" not in parsed.body
    assert "Thanks for getting in touch" in parsed.body


def test_strips_the_reply_prefix_from_the_subject():
    parsed = parse_email(REPLY_EMAIL.encode("utf-8"))
    assert not parsed.subject.lower().startswith("re:")
    assert parsed.subject == "Your membership renews on 15 January 2027"


def test_strips_the_forward_prefix_from_the_subject():
    parsed = parse_email(
        b"""From: a@example.com
To: b@example.com
Subject: Fwd: Your membership renews
Content-Type: text/plain

Body here.
"""
    )
    assert parsed.subject == "Your membership renews"


def test_reads_an_html_body():
    parsed = parse_email(
        b"""From: a@example.com
To: b@example.com
Subject: Notice
Content-Type: text/html

<html><body><p>Your renewal date is <b>2027-01-15</b>.</p>
<p>Call <a href="tel:5550148">555-0148</a></p></body></html>
"""
    )
    assert parsed.body_is_html
    assert "2027-01-15" in parsed.body
    assert "<b>" not in parsed.body


def test_an_empty_body_is_handled():
    parsed = parse_email(
        b"""From: a@example.com
Subject: Nothing
Content-Type: text/plain

"""
    )
    assert parsed.body == ""


def test_an_email_notice_reaches_the_pipeline(tmp_path):
    """An `.eml` is an ingestion path, not only a parser input."""
    path = tmp_path / "notice.eml"
    path.write_text(PLAIN_EMAIL, encoding="utf-8")

    result = process_document(path)

    assert result.ok
    assert result.item is not None
    assert result.item.provider == "Northwind Fitness"
    assert result.item.renewal_date == date(2027, 1, 15)
    assert result.item.reference == "1234-5678"
    assert result.deadlines[0].date == date(2027, 1, 15)
