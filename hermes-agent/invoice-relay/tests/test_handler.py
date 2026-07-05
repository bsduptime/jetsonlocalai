"""Handler dispatch: dry-run, confirmation gate, rate limiting, live calls."""

from __future__ import annotations

import os

import pytest

from hermes_greeninvoice import handler


class FakeClient:
    """Records upstream calls; returns canned data."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {"id": "doc_real", "number": "42"}

    def get(self, path, *, params=None):
        self.calls.append(("GET", path, None, params))
        return self._result

    def post(self, path, body, *, idempotent=True):
        self.calls.append(("POST", path, body, idempotent))
        return self._result

    def put(self, path, body, *, idempotent=True):
        self.calls.append(("PUT", path, body, idempotent))
        return self._result


def _issue_args(**over):
    a = {
        "type": 305,
        "client": {"id": "cli_1"},
        "income": [{"description": "Work", "quantity": 1, "price": 1000,
                    "currency": "ILS", "vatType": 0}],
        "currency": "ILS",
    }
    a.update(over)
    return a


def _req(op, args):
    return {"v": 1, "op": op, "request_id": "t", "args": args}


def _no_client():
    def _g():
        raise AssertionError("upstream must not be called in dry-run")
    return _g


def test_issue_requires_confirm(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("issue_invoice", _issue_args()),
                          get_client=_no_client())
    assert resp["ok"] is False
    assert resp["reason"] == "confirmation_required"


@pytest.mark.parametrize("confirm_val", ["true", "false", 1, "yes", {}, [1]])
def test_issue_confirm_must_be_literal_true(load_cfg, confirm_val):
    cfg = load_cfg()
    resp = handler.handle(
        cfg=cfg, caller="elena",
        request=_req("issue_invoice", _issue_args(confirm=confirm_val)),
        get_client=_no_client())
    assert resp["ok"] is False
    assert resp["reason"] == "confirmation_required"


def test_issue_dry_run_ok_and_counts(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("issue_invoice", _issue_args(confirm=True)),
                          get_client=_no_client())
    assert resp["ok"] is True
    assert resp["dry_run"] is True
    assert resp["rate"]["used_hour"] == 1
    assert resp["rate"]["remaining_day"] == cfg.limits["issue"][1] - 1


def test_issue_rate_limit_hourly(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_LIMIT_ISSUE_PER_HOUR", "2")
    cfg = load_cfg()
    for _ in range(2):
        ok = handler.handle(cfg=cfg, caller="elena",
                            request=_req("issue_invoice", _issue_args(confirm=True)),
                            get_client=_no_client())
        assert ok["ok"] is True
    blocked = handler.handle(cfg=cfg, caller="elena",
                             request=_req("issue_invoice", _issue_args(confirm=True)),
                             get_client=_no_client())
    assert blocked["ok"] is False
    assert blocked["reason"] == "rate_limit_exceeded"
    assert blocked["window"] == "hour"


def test_draft_dry_run_ok(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("draft_invoice", _issue_args()),
                          get_client=_no_client())
    assert resp["ok"] is True
    assert resp["result"]["preview"] is True


def test_quota_lists_all_classes(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("quota", {}), get_client=_no_client())
    assert resp["ok"] is True
    assert set(resp["quotas"]) == {
        "issue", "draft", "client_write", "expense_write", "expense_upload"}
    assert resp["env"] == "sandbox"


def test_unknown_op(load_cfg):
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("delete_everything", {}),
                          get_client=_no_client())
    assert resp["ok"] is False
    assert resp["reason"] == "unknown_op"


def test_live_issue_calls_upstream_with_no_client_email(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    fake = FakeClient()
    resp = handler.handle(
        cfg=cfg, caller="elena",
        request=_req("issue_invoice", _issue_args(confirm=True)),
        get_client=lambda: fake)
    assert resp["ok"] is True
    assert resp["dry_run"] is False
    method, path, body, _ = fake.calls[0]
    assert method == "POST" and path == "/documents"
    assert body["client"]["emails"] == []   # not distributed


def test_live_issue_email_to_client_populates(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    fake = FakeClient()
    args = _issue_args(confirm=True, email_to_client=True,
                       client={"id": "cli_1", "emails": ["c@x.com"]})
    handler.handle(cfg=cfg, caller="elena",
                   request=_req("issue_invoice", args),
                   get_client=lambda: fake)
    _, path, body, _ = fake.calls[0]
    assert body["client"]["emails"] == ["c@x.com"]


def test_live_upstream_4xx_frees_quota(load_cfg, monkeypatch):
    from hermes_greeninvoice.errors import UpstreamError

    monkeypatch.setenv("GI_DRY_RUN", "false")
    monkeypatch.setenv("GI_LIMIT_ISSUE_PER_HOUR", "1")
    cfg = load_cfg()

    class Failing:
        def post(self, path, body, *, idempotent=True):
            raise UpstreamError("api_error", detail="bad", status=422)

    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("issue_invoice", _issue_args(confirm=True)),
                          get_client=lambda: Failing())
    assert resp["ok"] is False
    assert resp["error"] == "upstream_failed"
    # 4xx => slot freed, so a retry is admitted (not rate-limited).
    fake = FakeClient()
    ok = handler.handle(cfg=cfg, caller="elena",
                        request=_req("issue_invoice", _issue_args(confirm=True)),
                        get_client=lambda: fake)
    assert ok["ok"] is True


def test_live_upstream_5xx_keeps_quota(load_cfg, monkeypatch):
    from hermes_greeninvoice.errors import UpstreamError

    monkeypatch.setenv("GI_DRY_RUN", "false")
    monkeypatch.setenv("GI_LIMIT_ISSUE_PER_HOUR", "1")
    cfg = load_cfg()

    class Failing5xx:
        def post(self, path, body, *, idempotent=True):
            raise UpstreamError("api_error", detail="boom", status=503)

    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("issue_invoice", _issue_args(confirm=True)),
                          get_client=lambda: Failing5xx())
    assert resp["error"] == "upstream_failed"
    # 5xx is ambiguous (doc might exist) => slot kept => next is blocked.
    blocked = handler.handle(cfg=cfg, caller="elena",
                             request=_req("issue_invoice", _issue_args(confirm=True)),
                             get_client=lambda: FakeClient())
    assert blocked["ok"] is False
    assert blocked["reason"] == "rate_limit_exceeded"
