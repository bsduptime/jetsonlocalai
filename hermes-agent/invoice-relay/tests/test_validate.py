"""Input validation + request-body builders."""

from __future__ import annotations

import pytest

from hermes_greeninvoice import validate
from hermes_greeninvoice.errors import InvalidInput


def _doc(**over):
    base = {
        "type": 305,
        "client": {"id": "cli_1"},
        "income": [{"description": "Consulting", "quantity": 1,
                    "price": 1000, "currency": "ILS", "vatType": 0}],
        "currency": "ILS",
    }
    base.update(over)
    return base


def test_issue_rejects_non_issuable_type():
    for t in (10, 300, 999):
        with pytest.raises(InvalidInput) as ei:
            validate.build_document(_doc(type=t), for_issue=True)
        assert ei.value.reason == "document_type_not_allowed"


def test_issue_accepts_issuable_types():
    for t in (305, 320, 400):
        body = validate.build_document(_doc(type=t), for_issue=True)
        assert body["type"] == t


def test_draft_allows_wider_types():
    for t in (10, 300, 305):
        body = validate.build_document(_doc(type=t), for_issue=False)
        assert body["type"] == t


def test_issue_without_email_forces_empty_emails():
    body = validate.build_document(_doc(client={"id": "cli_1"}), for_issue=True)
    assert body["client"]["emails"] == []


def test_email_to_client_requires_emails():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            _doc(email_to_client=True, client={"id": "cli_1"}), for_issue=True)
    assert ei.value.reason == "email_to_client_without_emails"


def test_email_to_client_with_emails_populates():
    body = validate.build_document(
        _doc(email_to_client=True,
             client={"id": "cli_1", "emails": ["a@b.com"]}),
        for_issue=True)
    assert body["client"]["emails"] == ["a@b.com"]


def test_draft_never_emails_even_with_emails():
    # email_to_client is ignored on drafts.
    body = validate.build_document(
        _doc(email_to_client=True,
             client={"id": "cli_1", "emails": ["a@b.com"]}),
        for_issue=False)
    assert body["client"]["emails"] == []


def test_income_required():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(_doc(income=[]), for_issue=True)
    assert ei.value.reason == "missing_field"


def test_income_quantity_must_be_positive():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            _doc(income=[{"description": "x", "quantity": 0, "price": 1,
                          "currency": "ILS"}]), for_issue=True)
    assert ei.value.reason == "invalid_quantity"


def test_income_currency_validated():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            _doc(income=[{"description": "x", "quantity": 1, "price": 1,
                          "currency": "SHEKEL"}]), for_issue=True)
    assert ei.value.reason == "invalid_currency"


def test_too_many_income_rows():
    rows = [{"description": "x", "quantity": 1, "price": 1, "currency": "ILS"}
            for _ in range(validate.MAX_INCOME_ROWS + 1)]
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(_doc(income=rows), for_issue=True)
    assert ei.value.reason == "too_many_income_rows"


def test_inline_client_defaults_to_not_added():
    body = validate.build_document(
        _doc(client={"name": "Acme Ltd"}), for_issue=True)
    assert body["client"]["name"] == "Acme Ltd"
    assert body["client"]["add"] is False


def test_linked_document_id_for_receipt():
    body = validate.build_document(
        _doc(type=400, linkedDocumentId="doc_99"), for_issue=True)
    assert body["linkedDocumentIds"] == ["doc_99"]


def test_email_to_client_truthy_string_does_not_email():
    # "false" is truthy in Python but must NOT enable distribution.
    body = validate.build_document(
        _doc(email_to_client="false",
             client={"id": "cli_1", "emails": ["a@b.com"]}),
        for_issue=True)
    assert body["client"]["emails"] == []


def test_add_truthy_string_does_not_persist():
    body = validate.build_document(
        _doc(client={"name": "Acme", "add": "false"}), for_issue=True)
    assert body["client"]["add"] is False


def test_add_ignored_on_draft_even_when_true():
    body = validate.build_document(
        _doc(client={"name": "Acme", "add": True}), for_issue=False)
    assert body["client"]["add"] is False


def test_add_true_persists_on_issue():
    body = validate.build_document(
        _doc(client={"name": "Acme", "add": True}), for_issue=True)
    assert body["client"]["add"] is True


def test_price_upper_bound():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            _doc(income=[{"description": "x", "quantity": 1,
                          "price": validate.MAX_UNIT_PRICE + 1,
                          "currency": "ILS"}]), for_issue=True)
    assert ei.value.reason == "invalid_price"


def test_quantity_upper_bound():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            _doc(income=[{"description": "x",
                          "quantity": validate.MAX_QUANTITY + 1,
                          "price": 1, "currency": "ILS"}]), for_issue=True)
    assert ei.value.reason == "invalid_quantity"


def test_client_create_requires_name():
    with pytest.raises(InvalidInput) as ei:
        validate.build_client_create({"emails": ["a@b.com"]})
    assert ei.value.reason == "missing_field"


def test_client_create_validates_email():
    with pytest.raises(InvalidInput) as ei:
        validate.build_client_create({"name": "X", "emails": ["not-an-email"]})
    assert ei.value.reason == "invalid_email"


def test_client_email_header_injection_rejected():
    with pytest.raises(InvalidInput):
        validate.build_client_create(
            {"name": "X", "emails": ["a@b.com\r\nBcc: evil@x.com"]})


def test_search_clamps_pagination_and_whitelists():
    body = validate.build_search(
        {"name": "acme", "page": 99999, "pageSize": 9999, "evil": "x"},
        allowed_fields={"name", "taxId"})
    assert body["name"] == "acme"
    assert "evil" not in body
    assert body["page"] == 1          # out-of-range -> default
    assert body["pageSize"] == 25     # out-of-range -> default
