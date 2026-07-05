"""Expense file upload: SSRF guard on the presigned URL + the multipart POST
(no bearer auth, no redirects)."""

from __future__ import annotations

import pytest

from hermes_greeninvoice import apiclient
from hermes_greeninvoice.errors import UpstreamError


def _public_addrinfo(host, port, *a, **k):
    return [(2, 1, 6, "", ("52.10.20.30", 443))]


def _private_addrinfo(host, port, *a, **k):
    return [(2, 1, 6, "", ("10.0.0.5", 443))]


# ---- _validate_upload_url (SSRF) -----------------------------------------

def test_validate_upload_url_accepts_s3(monkeypatch):
    monkeypatch.setattr(apiclient.socket, "getaddrinfo", _public_addrinfo)
    apiclient._validate_upload_url(
        "https://s3.eu-west-1.amazonaws.com/file-upload-service-uploaded")


@pytest.mark.parametrize("url", [
    "http://s3.eu-west-1.amazonaws.com/x",              # not https
    "https://user:pw@s3.eu-west-1.amazonaws.com/x",     # userinfo
    "https://evil.com/x",                                # host not allowlisted
    "https://s3.eu-west-1.amazonaws.com:8443/x",         # non-443 port
])
def test_validate_upload_url_rejects(monkeypatch, url):
    monkeypatch.setattr(apiclient.socket, "getaddrinfo", _public_addrinfo)
    with pytest.raises(UpstreamError):
        apiclient._validate_upload_url(url)


def test_validate_upload_url_rejects_private_ip(monkeypatch):
    monkeypatch.setattr(apiclient.socket, "getaddrinfo", _private_addrinfo)
    with pytest.raises(UpstreamError):
        apiclient._validate_upload_url("https://s3.eu-west-1.amazonaws.com/x")


# ---- upload_file_to_s3 (multipart, no auth) ------------------------------

class _FakeResp:
    def getcode(self):
        return 204

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_client(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_API_KEY_ID", "id")
    monkeypatch.setenv("GI_API_KEY_SECRET", "secret")
    cfg = load_cfg()
    return apiclient.GreenInvoiceClient(cfg)


def test_upload_file_to_s3_multipart_no_auth(load_cfg, monkeypatch):
    client = _make_client(load_cfg, monkeypatch)
    monkeypatch.setattr(apiclient, "_validate_upload_url", lambda url: None)

    captured = {}

    def _fake_open(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr(apiclient._NO_REDIRECT_OPENER, "open", _fake_open)

    client.upload_file_to_s3(
        "https://s3.eu-west-1.amazonaws.com/bucket",
        {"key": "the-key", "policy": "the-policy", "X-Amz-Signature": "sig"},
        filename="receipt.pdf", content_type="application/pdf",
        data=b"%PDF-1.4 fake")

    assert captured["method"] == "POST"
    # No Authorization header — the presigned fields are the auth.
    assert not any(h.lower() == "authorization" for h in captured["headers"])
    ct = next(v for k, v in captured["headers"].items() if k.lower() == "content-type")
    assert ct.startswith("multipart/form-data; boundary=")
    body = captured["data"]
    assert b'name="key"' in body and b"the-key" in body
    assert b'name="file"; filename="receipt.pdf"' in body
    assert b"%PDF-1.4 fake" in body
    # file part is last (closing boundary follows the file data)
    assert body.rstrip().endswith(b"--")


def test_upload_file_to_s3_rejects_bad_status(load_cfg, monkeypatch):
    client = _make_client(load_cfg, monkeypatch)
    monkeypatch.setattr(apiclient, "_validate_upload_url", lambda url: None)

    class _Err(_FakeResp):
        def getcode(self):
            return 403

    monkeypatch.setattr(apiclient._NO_REDIRECT_OPENER, "open",
                        lambda req, timeout=None: _Err())
    with pytest.raises(UpstreamError):
        client.upload_file_to_s3(
            "https://s3.eu-west-1.amazonaws.com/bucket", {"key": "k"},
            filename="r.pdf", content_type="application/pdf", data=b"x")
