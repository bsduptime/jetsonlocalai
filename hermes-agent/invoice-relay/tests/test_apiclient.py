"""apiclient retry policy: non-idempotent POSTs must not be retried on 5xx."""

from __future__ import annotations

import pytest

from hermes_greeninvoice import config as _cfg
from hermes_greeninvoice.apiclient import GreenInvoiceClient
from hermes_greeninvoice.errors import UpstreamError


@pytest.fixture
def client(_isolate_env, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    monkeypatch.setenv("GI_API_KEY_ID", "id")
    monkeypatch.setenv("GI_API_KEY_SECRET", "secret")
    cfg = _cfg.load_config()
    c = GreenInvoiceClient(cfg)
    # Avoid hitting the token endpoint.
    monkeypatch.setattr(c._tokens, "get", lambda force=False: "tok")
    return c


def _stub_http(client, monkeypatch, status, data=None):
    calls = []

    def fake_http(method, url, *, headers, body):
        calls.append((method, url))
        return status, (data if data is not None else {"errorMessage": "x"})

    monkeypatch.setattr(client, "_http", fake_http)
    return calls


def test_non_idempotent_post_not_retried_on_5xx(client, monkeypatch):
    calls = _stub_http(client, monkeypatch, 503)
    with pytest.raises(UpstreamError):
        client.post("/documents", {"type": 305}, idempotent=False)
    assert len(calls) == 1  # NO retry — a 2nd POST could double-issue


def test_idempotent_get_retried_on_5xx(client, monkeypatch):
    calls = _stub_http(client, monkeypatch, 503)
    with pytest.raises(UpstreamError):
        client.get("/documents/abc")
    assert len(calls) == 4  # MAX_RETRIES + 1


def test_non_idempotent_post_retried_on_429(client, monkeypatch):
    # 429 == provably not processed, so retrying is safe even for POST.
    calls = _stub_http(client, monkeypatch, 429)
    with pytest.raises(UpstreamError):
        client.post("/documents", {"type": 305}, idempotent=False)
    assert len(calls) == 4


def test_2xx_returns_data(client, monkeypatch):
    _stub_http(client, monkeypatch, 200, {"id": "doc_1"})
    assert client.post("/documents", {}, idempotent=False) == {"id": "doc_1"}
