from __future__ import annotations

import pytest

from hermes_email_pkg import headers
from hermes_email_pkg.errors import InvalidInput


def test_validates_bare_address_for_to():
    out = headers.validate_address_field("to", "alice@example.com",
                                         allow_display_name=False)
    assert out == "alice@example.com"


def test_rejects_display_name_in_to():
    with pytest.raises(InvalidInput) as ei:
        headers.validate_address_field("to", "Alice <alice@example.com>",
                                       allow_display_name=False)
    assert ei.value.reason == "display_name_not_allowed"


def test_allows_display_name_in_from():
    raw = "David Klippel <david@example.com>"
    out = headers.validate_address_field("from", raw, allow_display_name=True)
    assert out == raw


@pytest.mark.parametrize("evil", [
    "alice@example.com\r\nBcc: attacker@evil",
    "alice@example.com\nBcc: attacker@evil",
    "alice\x00@example.com",
])
def test_rejects_header_injection_in_to(evil):
    with pytest.raises(InvalidInput) as ei:
        headers.validate_address_field("to", evil, allow_display_name=False)
    assert ei.value.reason in {"header_injection", "invalid_email"}


@pytest.mark.parametrize("evil", [
    "Display\r\nBcc: attacker@evil <a@example.com>",
    "Display\nx <a@example.com>",
])
def test_rejects_header_injection_in_display_name(evil):
    with pytest.raises(InvalidInput) as ei:
        headers.validate_address_field("from", evil, allow_display_name=True)
    assert ei.value.reason == "header_injection"


def test_subject_rejects_newlines():
    with pytest.raises(InvalidInput) as ei:
        headers.validate_subject("hi\nBcc: attacker@evil")
    assert ei.value.reason == "header_injection"


def test_subject_too_long():
    with pytest.raises(InvalidInput) as ei:
        headers.validate_subject("a" * 201)
    assert ei.value.reason == "subject_too_long"


def test_subject_accepts_unicode_and_length_200():
    out = headers.validate_subject("h" * 200)
    assert out == "h" * 200


def test_address_only_strips_display_name():
    assert headers.address_only("Alice <ALICE@example.COM>") == "alice@example.com"


@pytest.mark.parametrize("bad", ["", "not-an-email", "no-at-sign", "no@dot",
                                 "two@@signs.com", "trailing.@dot.com"])
def test_invalid_email_rejected(bad):
    with pytest.raises(InvalidInput) as ei:
        headers.validate_address_field("to", bad, allow_display_name=False)
    assert ei.value.reason in {"invalid_email", "missing_field"}
