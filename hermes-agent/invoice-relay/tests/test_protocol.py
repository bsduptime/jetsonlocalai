"""End-to-end protocol round-trip through the UDS daemon (dry-run).

Uses the thin client (hermes_greeninvoice_client) exactly as the Hermes
plugin would, over a real Unix socket, with SO_PEERCRED caller resolution.
"""

from __future__ import annotations

import socket

import pytest

import hermes_greeninvoice_client as client


def _issue_args(**over):
    a = {
        "type": 305,
        "client": {"id": "cli_1"},
        "income": [{"description": "Work", "quantity": 1, "price": 1000,
                    "currency": "ILS"}],
        "currency": "ILS",
    }
    a.update(over)
    return a


def test_quota_roundtrip(daemon_process):
    resp = client.call("quota", {}, socket_path=daemon_process)
    assert resp["ok"] is True
    assert resp["dry_run"] is True
    assert "issue" in resp["quotas"]


def test_draft_roundtrip(daemon_process):
    resp = client.call("draft_invoice", _issue_args(), socket_path=daemon_process)
    assert resp["ok"] is True
    assert resp["result"]["preview"] is True


def test_issue_without_confirm_denied(daemon_process):
    resp = client.call("issue_invoice", _issue_args(), socket_path=daemon_process)
    assert resp["ok"] is False
    assert resp["reason"] == "confirmation_required"


def test_issue_with_confirm_dry_run(daemon_process):
    resp = client.call("issue_invoice", _issue_args(confirm=True),
                       socket_path=daemon_process)
    assert resp["ok"] is True
    assert resp["dry_run"] is True
    assert resp["rate"]["used_hour"] == 1


def test_invalid_input_surfaced(daemon_process):
    resp = client.call("issue_invoice",
                       _issue_args(confirm=True, type=999),
                       socket_path=daemon_process)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "document_type_not_allowed"


def test_unknown_op_protocol_error(daemon_process):
    resp = client.call("rm_rf", {}, socket_path=daemon_process)
    assert resp["ok"] is False
    assert resp["reason"] == "unknown_op"


def test_version_mismatch(daemon_process):
    # Hand-craft a bad-version envelope.
    import json
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(daemon_process)
    sock.sendall((json.dumps({"v": 2, "op": "quota", "request_id": "x",
                              "args": {}}) + "\n").encode())
    line = b""
    while b"\n" not in line:
        line += sock.recv(4096)
    sock.close()
    resp = json.loads(line.split(b"\n", 1)[0])
    assert resp["ok"] is False
    assert resp["reason"] == "version_mismatch"


def test_malformed_json(daemon_process):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(daemon_process)
    sock.sendall(b"{not json\n")
    line = b""
    while b"\n" not in line:
        line += sock.recv(4096)
    sock.close()
    import json
    resp = json.loads(line.split(b"\n", 1)[0])
    assert resp["ok"] is False
    assert resp["reason"] == "malformed_json"
