from __future__ import annotations

from email import message_from_bytes

import pytest

from hermes_email_pkg import attachments, transport
from hermes_email_pkg.errors import PreSendError


def _rendered(html=None, attachs=None):
    return transport.RenderedEmail(
        to="alice@example.com",
        from_="Hermes <test@example.com>",
        reply_to=None,
        subject="hello",
        text="hi there",
        html=html,
        attachments=attachs or [],
    )


def test_dryrun_writes_eml_and_returns_id(tmp_path):
    dryrun_dir = tmp_path / "dryrun"
    t = transport.DryRunTransport(dryrun_dir)
    mid = t.send(_rendered())
    assert mid.startswith("dryrun-")
    files = list(dryrun_dir.iterdir())
    assert len(files) == 1
    # Parse the saved .eml and check headers
    eml = message_from_bytes(files[0].read_bytes())
    assert eml["To"] == "alice@example.com"
    assert eml["Subject"] == "hello"
    assert eml["From"].startswith("Hermes")


def test_dryrun_with_attachment(stage_fixture, tmp_path):
    fixture = stage_fixture("tiny.pdf")
    a = attachments.load_attachment(
        str(fixture),
        max_bytes=100_000,
        allowed_prefixes=[str(tmp_path) + "/"],
    )
    t = transport.DryRunTransport(tmp_path / "dryrun")
    mid = t.send(_rendered(attachs=[a]))
    assert mid.startswith("dryrun-")
    files = list((tmp_path / "dryrun").iterdir())
    eml = message_from_bytes(files[0].read_bytes())
    parts = list(eml.walk())
    # Expect at least the multipart container, the text body, and the pdf part
    pdfs = [p for p in parts if p.get_content_type() == "application/pdf"]
    assert len(pdfs) == 1


def test_make_transport_dry_run_short_circuits(tmp_path):
    class Cfg:
        dryrun_dir = tmp_path / "d"
        resend_api_key = "anything"
        smtp_host = "anything"; smtp_port = 587
        smtp_username = "u"; smtp_password = "p"; smtp_starttls = True

    cfg = Cfg()
    t = transport.make_transport(dry_run=True, transport_name="resend", cfg=cfg)
    assert isinstance(t, transport.DryRunTransport)


def test_smtp_missing_host_raises(tmp_path):
    with pytest.raises(PreSendError):
        transport.SMTPTransport(host="", port=587, username=None,
                                password=None, starttls=True)


def test_resend_missing_key_raises():
    with pytest.raises(PreSendError):
        transport.ResendTransport(api_key="")


def test_resend_module_missing_is_presend(monkeypatch, tmp_path):
    """If `resend` isn't installed, .send() raises PreSendError (so we mark
    failed_pre_send, not unknown_post_send)."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "resend":
            raise ImportError("no module named 'resend'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    t = transport.ResendTransport(api_key="x")
    with pytest.raises(PreSendError):
        t.send(_rendered())


def test_resend_attachment_is_base64(monkeypatch, stage_fixture, tmp_path):
    """Spy on resend.Emails.send and confirm we pass base64-encoded content,
    not a list of ints (Codex Finding 6)."""
    import sys, types
    fake_resend = types.ModuleType("resend")
    captured = {}

    class _Emails:
        @staticmethod
        def send(params):
            captured["params"] = params
            return {"id": "rs-123"}

    fake_resend.Emails = _Emails
    fake_resend.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake_resend)

    fixture = stage_fixture("tiny.pdf")
    a = attachments.load_attachment(
        str(fixture),
        max_bytes=100_000,
        allowed_prefixes=[str(tmp_path) + "/"],
    )
    t = transport.ResendTransport(api_key="rk_test")
    mid = t.send(_rendered(attachs=[a]))
    assert mid == "rs-123"
    params = captured["params"]
    assert params["from"] == "Hermes <test@example.com>"
    assert params["to"] == ["alice@example.com"]
    assert "attachments" in params
    att = params["attachments"][0]
    assert isinstance(att["content"], str)               # base64 string, NOT list
    assert att["content_type"] == "application/pdf"
    # Decoding the base64 must reproduce the file bytes
    import base64 as _b
    assert _b.b64decode(att["content"]) == fixture.read_bytes()


def test_resend_validation_error_is_presend(monkeypatch):
    import sys, types
    fake_resend = types.ModuleType("resend")

    class ResendValidationError(RuntimeError):
        pass

    class _Emails:
        @staticmethod
        def send(params):
            raise ResendValidationError("invalid From")

    fake_resend.Emails = _Emails
    fake_resend.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake_resend)
    t = transport.ResendTransport(api_key="rk_test")
    with pytest.raises(PreSendError):
        t.send(_rendered())


def test_resend_5xx_is_post_send_unknown(monkeypatch):
    """5xx / network errors propagate as bare Exception so the handler can
    mark them unknown_post_send."""
    import sys, types
    fake_resend = types.ModuleType("resend")

    class _Emails:
        @staticmethod
        def send(params):
            class _Boom(RuntimeError): pass
            err = _Boom("server is on fire")
            err.status_code = 503
            raise err

    fake_resend.Emails = _Emails
    fake_resend.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake_resend)
    t = transport.ResendTransport(api_key="rk_test")
    with pytest.raises(RuntimeError):
        t.send(_rendered())


def test_smtp_auth_failure_is_presend(monkeypatch):
    import smtplib

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host = host

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, user, pw):
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")
        def send_message(self, em): pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    t = transport.SMTPTransport(host="smtp.example.com", port=587,
                                username="u", password="p", starttls=True)
    with pytest.raises(PreSendError):
        t.send(_rendered())


def test_smtp_connect_failure_is_presend(monkeypatch):
    import smtplib

    def boom(*a, **kw):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    t = transport.SMTPTransport(host="smtp.example.com", port=587,
                                username="u", password="p", starttls=True)
    with pytest.raises(PreSendError):
        t.send(_rendered())


def test_smtp_port_465_uses_implicit_tls(monkeypatch):
    """Codex F8: port 465 must use SMTP_SSL regardless of the STARTTLS flag."""
    import smtplib

    used = []

    class FakeSMTPSSL:
        def __init__(self, *a, **kw): used.append("ssl")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, em): pass

    class FakeSMTP:
        def __init__(self, *a, **kw): used.append("plain")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, em): pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    # Even with starttls=True (which is the wrong combo for 465), we use SMTP_SSL
    t = transport.SMTPTransport(host="x", port=465, username="u",
                                password="p", starttls=True)
    t.send(_rendered())
    assert used == ["ssl"]


def test_smtp_port_587_starttls_false_does_not_call_starttls(monkeypatch):
    """If STARTTLS is disabled on a non-465 port, we should NOT upgrade."""
    import smtplib

    starttls_called = []

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, context=None): starttls_called.append(True)
        def login(self, u, p): pass
        def send_message(self, em): pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    t = transport.SMTPTransport(host="x", port=587, username="u",
                                password="p", starttls=False)
    t.send(_rendered())
    assert starttls_called == []


def test_smtp_success_returns_message_id(monkeypatch):
    import smtplib

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, em):
            sent["em"] = em

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    t = transport.SMTPTransport(host="smtp.example.com", port=587,
                                username="u", password="p", starttls=True)
    mid = t.send(_rendered())
    assert mid.startswith("<") and mid.endswith(">")
    assert sent["em"]["To"] == "alice@example.com"
