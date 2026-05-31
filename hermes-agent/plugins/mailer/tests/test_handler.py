"""Thin-client tests for the mailer plugin.

The plugin no longer enforces policy in-process — it validates field types
+ attachment paths, frames a JSON envelope, talks to the daemon over a UDS,
and returns the daemon's response with the protocol-version key stripped.
We exercise exactly those responsibilities here. Daemon responses are
simulated by monkeypatching `_client._roundtrip`; the `daemon_unreachable`
path uses the real (missing) socket from conftest.
"""

from __future__ import annotations

import json

from hermes_email_pkg import _client, handler


def send(args):
    return json.loads(handler.send_email(args))


def contacts():
    return json.loads(handler.list_contacts({}))


# --------------------------------------------------------------------------
# send_email — field + attachment pre-validation (no socket needed)
# --------------------------------------------------------------------------

def test_attachments_must_be_a_list(_isolate_env):
    resp = send({"to": "a@example.com", "subject": "s", "body": "b",
                 "attachments": "not-a-list"})
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "invalid_field_type"
    assert resp["detail"] == "attachments"


def test_attachment_items_must_be_strings(_isolate_env):
    resp = send({"to": "a@example.com", "subject": "s", "body": "b",
                 "attachments": [123]})
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "attachment_invalid_path"


def test_attachment_outside_allowed_prefix(_isolate_env, fixtures_dir):
    # Fixtures live in the plugin source dir, not under /tmp or tmp_path.
    resp = send({"to": "a@example.com", "subject": "s", "body": "b",
                 "attachments": [str(fixtures_dir / "tiny.pdf")]})
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "attachment_outside_allowed_prefixes"


def test_attachment_not_found(_isolate_env):
    resp = send({"to": "a@example.com", "subject": "s", "body": "b",
                 "attachments": ["/tmp/definitely-missing-xyzzy.pdf"]})
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "attachment_not_found"


# --------------------------------------------------------------------------
# send_email — daemon round-trip (mocked) + unreachable
# --------------------------------------------------------------------------

def test_send_strips_protocol_version(monkeypatch, _isolate_env):
    captured = {}

    def fake_roundtrip(envelope, *, sock_path, timeout_seconds):
        captured["envelope"] = envelope
        return {"v": 1, "request_id": envelope["request_id"], "ok": True,
                "status": "sent", "to": "a@example.com", "message_id": "m1",
                "remaining_today": 4, "limit": 5, "resets_at": "X"}

    monkeypatch.setattr(_client, "_roundtrip", fake_roundtrip)
    resp = send({"to": "a@example.com", "subject": "hi", "body": "yo"})
    assert resp["ok"] is True
    assert "v" not in resp                    # daemon's version key removed
    assert captured["envelope"]["op"] == "send"
    assert captured["envelope"]["to"] == "a@example.com"


def test_send_daemon_unreachable(_isolate_env):
    # conftest points HERMES_MAILER_SOCKET at a missing path.
    resp = send({"to": "a@example.com", "subject": "hi", "body": "yo"})
    assert resp["ok"] is False
    assert resp["error"] == "transport_failed"
    assert resp["reason"] == "daemon_unreachable"


def test_send_always_returns_json_string(_isolate_env):
    out = handler.send_email({"to": "a@example.com", "subject": "s", "body": "b"})
    assert isinstance(out, str)
    json.loads(out)


# --------------------------------------------------------------------------
# list_contacts
# --------------------------------------------------------------------------

def test_list_contacts_sends_contacts_op_and_strips_v(monkeypatch, _isolate_env):
    captured = {}

    def fake_roundtrip(envelope, *, sock_path, timeout_seconds):
        captured["envelope"] = envelope
        return {"v": 1, "request_id": envelope["request_id"], "ok": True,
                "resets_at": "X",
                "contacts": [{"email": "yoram@dbexpert.ai", "name": "Yoram",
                              "aliases": ["yoram"], "note": None,
                              "daily_limit": 5, "remaining_today": 5}]}

    monkeypatch.setattr(_client, "_roundtrip", fake_roundtrip)
    resp = contacts()
    assert captured["envelope"]["op"] == "contacts"
    assert "to" not in captured["envelope"]          # no send-only fields
    assert resp["ok"] is True
    assert "v" not in resp
    assert resp["contacts"][0]["aliases"] == ["yoram"]


def test_list_contacts_daemon_unreachable(_isolate_env):
    resp = contacts()
    assert resp["ok"] is False
    assert resp["error"] == "transport_failed"
    assert resp["reason"] == "daemon_unreachable"


def test_list_contacts_always_returns_json_string(monkeypatch, _isolate_env):
    monkeypatch.setattr(
        _client, "_roundtrip",
        lambda e, *, sock_path, timeout_seconds: {"v": 1, "ok": True,
                                                  "contacts": []})
    out = handler.list_contacts({})
    assert isinstance(out, str)
    json.loads(out)
