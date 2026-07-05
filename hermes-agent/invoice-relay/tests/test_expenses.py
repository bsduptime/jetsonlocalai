"""Expense ops: validation, dry-run routing, the confirm-gated close, the
create-as-Open guarantee, and the framed file-upload relay."""

from __future__ import annotations

import base64

import pytest

from hermes_greeninvoice import handler, validate
from hermes_greeninvoice.errors import InvalidInput, UpstreamError


# ---- fakes ----------------------------------------------------------------

class FakeExpenseClient:
    """Records every upstream verb the expense ops may use."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {"id": "exp_1", "status": 10}
        self.uploads = []

    def get(self, path, *, params=None):
        self.calls.append(("GET", path, None, params))
        return self._result

    def post(self, path, body, *, idempotent=True):
        self.calls.append(("POST", path, body, idempotent))
        return self._result

    def put(self, path, body, *, idempotent=True):
        self.calls.append(("PUT", path, body, idempotent))
        return self._result

    def request(self, method, path, *, body=None, params=None, idempotent=True,
                base_url=None):
        self.calls.append((method, path, body, idempotent))
        return self._result

    # upload two-call flow
    def get_upload_url(self, *, source=5):
        self.calls.append(("GET", "/file-upload/v1/url", {"source": source}, True))
        return {"url": "https://s3.eu-west-1.amazonaws.com/file-upload-service-uploaded",
                "fields": {"key": "k", "policy": "p", "X-Amz-Signature": "sig"}}

    def upload_file_to_s3(self, url, fields, *, filename, content_type, data):
        self.uploads.append((url, fields, filename, content_type, len(data)))


def _req(op, args):
    return {"v": 1, "op": op, "request_id": "t", "args": args}


def _expense_args(**over):
    a = {
        "documentType": 20,
        "amount": 117.0,
        "vat": 17.0,
        "currency": "ILS",
        "number": "INV-42",
        "date": "2026-06-15",
        "supplier": {"name": "Cellcom"},
    }
    a.update(over)
    return a


# ---- validate.build_expense ----------------------------------------------

def test_build_expense_happy():
    body = validate.build_expense(_expense_args())
    assert body["documentType"] == 20
    assert body["amount"] == 117.0
    assert body["vat"] == 17.0
    assert body["currency"] == "ILS"
    assert body["supplier"]["name"] == "Cellcom"
    assert body["active"] is True
    # No path here ever sets status/report fields.
    assert "status" not in body and "close" not in body


def test_build_expense_defaults_documenttype_other():
    body = validate.build_expense({"amount": 10, "supplier": {"name": "X"}})
    assert body["documentType"] == 40


@pytest.mark.parametrize("bad", [
    {"amount": 10, "supplier": {"name": "X"}, "documentType": 99},
    {"amount": -1, "supplier": {"name": "X"}},
    {"amount": 10 ** 12, "supplier": {"name": "X"}},
    {"amount": 10, "supplier": {}},               # supplier needs name or id
    {"amount": 10, "supplier": {"name": "X"}, "vatType": 7},
    {"amount": 10, "supplier": {"name": "X"}, "paymentType": 99},
    {"amount": 10, "supplier": {"name": "X"}, "date": "2026-13-01"},
    {"supplier": {"name": "X"}},                   # amount required
])
def test_build_expense_rejections(bad):
    with pytest.raises(InvalidInput):
        validate.build_expense(bad)


def test_build_expense_existing_supplier_id():
    body = validate.build_expense({"amount": 5, "supplier": {"id": "sup_9"}})
    assert body["supplier"] == {"id": "sup_9"}


def test_build_expense_active_not_caller_controllable():
    # A direct daemon caller must not be able to hide an expense from review.
    body = validate.build_expense(_expense_args(active=False))
    assert body["active"] is True


def test_build_supplier_create():
    body = validate.build_supplier_create({"name": "Acme", "taxId": "123", "country": "IL"})
    assert body["name"] == "Acme" and body["taxId"] == "123"
    with pytest.raises(InvalidInput):
        validate.build_supplier_create({})            # name required
    with pytest.raises(InvalidInput):
        validate.build_supplier_create({"name": "A", "country": "ISR"})  # 2-letter


# ---- validate.build_upload_meta ------------------------------------------

def test_build_upload_meta_happy():
    m = validate.build_upload_meta(
        {"filename": "receipt.pdf", "content_type": "application/pdf", "byte_len": 1000},
        max_file_bytes=10 * 1024 * 1024)
    assert m == {"filename": "receipt.pdf", "content_type": "application/pdf",
                 "byte_len": 1000}


def test_build_upload_meta_strips_content_type_params():
    # A ;-parameter tail (incl. any CRLF) must be dropped — it would otherwise
    # ride into the multipart Content-Type header (injection).
    m = validate.build_upload_meta(
        {"filename": "r.pdf", "content_type": "application/pdf;\r\nX-Injected: y",
         "byte_len": 10}, max_file_bytes=1024)
    assert m["content_type"] == "application/pdf"


@pytest.mark.parametrize("bad", [
    {"filename": "x.exe", "content_type": "application/pdf", "byte_len": 10},
    {"filename": "../etc/passwd", "content_type": "application/pdf", "byte_len": 10},
    {"filename": 'a".pdf', "content_type": "application/pdf", "byte_len": 10},
    {"filename": "x.pdf", "content_type": "text/html", "byte_len": 10},
    {"filename": "x.pdf", "content_type": "application/pdf", "byte_len": 0},
    {"filename": "x.pdf", "content_type": "application/pdf", "byte_len": 99999999},
    {"filename": "x.pdf", "content_type": "application/pdf", "byte_len": "10"},
])
def test_build_upload_meta_rejections(bad):
    with pytest.raises(InvalidInput):
        validate.build_upload_meta(bad, max_file_bytes=10 * 1024 * 1024)


# ---- handler dry-run routing ---------------------------------------------

def _no_client():
    def _g():
        raise AssertionError("upstream must not be called in dry-run")
    return _g


def test_create_expense_dry_run_open(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("create_expense", _expense_args()),
                          get_client=_no_client())
    assert resp["ok"] is True and resp["dry_run"] is True
    assert resp["result"]["status"] == 10           # created OPEN
    assert resp["rate"]["action_class"] == "expense_write"


def test_close_expense_requires_confirm(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("close_expense", {"id": "exp_1"}),
                          get_client=_no_client())
    assert resp["ok"] is False
    assert resp["reason"] == "confirmation_required"


@pytest.mark.parametrize("confirm_val", ["true", 1, "yes", {}, [1], 0, None])
def test_close_expense_confirm_must_be_literal_true(load_cfg, confirm_val):
    cfg = load_cfg()
    resp = handler.handle(
        cfg=cfg, caller="elena",
        request=_req("close_expense", {"id": "exp_1", "confirm": confirm_val}),
        get_client=_no_client())
    assert resp["ok"] is False and resp["reason"] == "confirmation_required"


def test_close_expense_uses_issue_budget(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(
        cfg=cfg, caller="elena",
        request=_req("close_expense", {"id": "exp_1", "confirm": True}),
        get_client=_no_client())
    assert resp["ok"] is True and resp["dry_run"] is True
    # close shares the tight `issue` class, not a separate expense budget.
    assert resp["rate"]["action_class"] == "issue"


def test_search_expenses_dry_run_read(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(
        cfg=cfg, caller="elena",
        request=_req("search_expenses", {"supplierName": "Cellcom", "reported": False}),
        get_client=_no_client())
    assert resp["ok"] is True and resp["result"]["total"] == 0


def test_upload_expense_file_dry_run(load_cfg):
    cfg = load_cfg()
    args = {"filename": "r.pdf", "content_type": "application/pdf", "byte_len": 5}
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("upload_expense_file", args),
                          get_client=_no_client(), file_body=b"hello")
    assert resp["ok"] is True and resp["dry_run"] is True
    assert resp["rate"]["action_class"] == "expense_upload"


# ---- live routing (fake upstream) ----------------------------------------

def test_live_create_expense_posts_expenses(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    fake = FakeExpenseClient()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("create_expense", _expense_args()),
                          get_client=lambda: fake)
    assert resp["ok"] is True
    method, path, body, idem = fake.calls[0]
    assert method == "POST" and path == "/expenses" and idem is False
    assert body["amount"] == 117.0


def test_live_close_expense_calls_close(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    fake = FakeExpenseClient(result={"id": "exp_1", "status": 20})
    resp = handler.handle(
        cfg=cfg, caller="elena",
        request=_req("close_expense", {"id": "exp_1", "confirm": True}),
        get_client=lambda: fake)
    assert resp["ok"] is True
    method, path, _, idem = fake.calls[0]
    assert method == "POST" and path == "/expenses/exp_1/close" and idem is False


def test_live_delete_expense_calls_delete(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    fake = FakeExpenseClient(result={"id": "exp_1", "deleted": True})
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("delete_expense", {"id": "exp_1"}),
                          get_client=lambda: fake)
    assert resp["ok"] is True
    method, path, _, _ = fake.calls[0]
    assert method == "DELETE" and path == "/expenses/exp_1"


def test_live_upload_two_call_flow(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    fake = FakeExpenseClient()
    args = {"filename": "r.pdf", "content_type": "application/pdf", "byte_len": 5}
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("upload_expense_file", args),
                          get_client=lambda: fake, file_body=b"hello")
    assert resp["ok"] is True
    assert ("GET", "/file-upload/v1/url", {"source": 5}, True) in fake.calls
    assert fake.uploads and fake.uploads[0][2] == "r.pdf" and fake.uploads[0][4] == 5


def test_upload_length_mismatch_rejected(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    fake = FakeExpenseClient()
    args = {"filename": "r.pdf", "content_type": "application/pdf", "byte_len": 5}
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("upload_expense_file", args),
                          get_client=lambda: fake, file_body=b"hi")   # len 2 != 5
    assert resp["ok"] is False and resp["error"] == "upstream_failed"
    assert not fake.uploads


# ---- the create-as-Open guarantee ----------------------------------------

def test_only_close_expense_can_report(load_cfg, monkeypatch):
    """No expense op EXCEPT close_expense may ever hit /close (status 20)."""
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    ops = [
        ("create_expense", _expense_args()),
        ("delete_expense", {"id": "exp_1"}),
        ("get_expense", {"id": "exp_1"}),
        ("search_expenses", {"supplierName": "X"}),
        ("create_supplier", {"name": "Y"}),
        ("search_suppliers", {"name": "Y"}),
        ("get_classifications", {}),
    ]
    for op, args in ops:
        fake = FakeExpenseClient()
        handler.handle(cfg=cfg, caller="elena", request=_req(op, args),
                       get_client=lambda f=fake: f)
        for _method, path, _body, _idem in fake.calls:
            assert "/close" not in path, f"{op} unexpectedly hit {path}"
