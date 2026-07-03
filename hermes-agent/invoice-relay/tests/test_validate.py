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
    pay = [{"date": "2026-06-03", "type": 1, "price": 1170, "currency": "ILS"}]
    cases = {
        305: _doc(type=305),                                  # income only
        320: _doc(type=320, payment=pay),                     # income + payment
        400: {"type": 400, "client": {"id": "cli_1"},         # payment only
              "currency": "ILS", "payment": pay},
    }
    for t, args in cases.items():
        body = validate.build_document(args, for_issue=True)
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
        {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS",
         "payment": [{"date": "2026-06-03", "type": 1, "price": 100,
                      "currency": "ILS"}], "linkedDocumentId": "doc_99"},
        for_issue=True)
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


def _payment(**over):
    p = {"date": "2026-06-03", "type": 4, "price": 1180, "currency": "ILS"}
    p.update(over)
    return p


def test_receipt_400_requires_payment_but_not_income():
    # Standalone receipt: payment required, income optional.
    body = validate.build_document(
        {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS",
         "payment": [_payment()]}, for_issue=True)
    assert body["type"] == 400
    assert body["payment"][0]["type"] == 4
    assert "income" not in body  # no line items needed


def test_receipt_400_without_payment_rejected():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS"},
            for_issue=True)
    assert ei.value.reason == "missing_field"
    assert ei.value.detail == "payment"


def test_receipt_400_linked_to_invoice():
    body = validate.build_document(
        {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS",
         "payment": [_payment()], "linkedDocumentId": "inv_99"},
        for_issue=True)
    assert body["linkedDocumentIds"] == ["inv_99"]


def test_320_requires_both_income_and_payment():
    base = {"type": 320, "client": {"id": "cli_1"}, "currency": "ILS",
            "income": [{"description": "x", "quantity": 1, "price": 1000,
                        "currency": "ILS"}]}
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(base, for_issue=True)  # no payment
    assert ei.value.reason == "missing_field" and ei.value.detail == "payment"
    body = validate.build_document({**base, "payment": [_payment()]}, for_issue=True)
    assert body["income"] and body["payment"]


def test_payment_rejected_on_305():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(_doc(payment=[_payment()]), for_issue=True)
    assert ei.value.reason == "payment_not_allowed_for_type"


def test_invalid_payment_type_rejected():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS",
             "payment": [_payment(type=99)]}, for_issue=True)
    assert ei.value.reason == "invalid_payment_type"


def test_payment_optional_details_pass_through():
    body = validate.build_document(
        {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS",
         "payment": [_payment(type=2, chequeNum="00123", bankName="Leumi")]},
        for_issue=True)
    row = body["payment"][0]
    assert row["chequeNum"] == "00123" and row["bankName"] == "Leumi"


def test_bit_payment_app_passes_through():
    # Money received via Bit: payment app (type 10) + appType 1.
    body = validate.build_document(
        {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS",
         "payment": [_payment(type=10, appType=1)]},
        for_issue=True)
    row = body["payment"][0]
    assert row["type"] == 10 and row["appType"] == 1


def test_invalid_app_type_rejected():
    with pytest.raises(InvalidInput) as ei:
        validate.build_document(
            {"type": 400, "client": {"id": "cli_1"}, "currency": "ILS",
             "payment": [_payment(type=10, appType=99)]},
            for_issue=True)
    assert ei.value.reason == "invalid_app_type"


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
