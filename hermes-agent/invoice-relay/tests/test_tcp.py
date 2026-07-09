"""Loopback-TCP transport (the Windows path): token auth + framed uploads.

Mirrors the UDS protocol tests but over tcp://127.0.0.1 with caller
identity coming from GI_CALLER_TOKEN_<name> instead of peer credentials.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

import hermes_greeninvoice_client as client

TOKEN = "tcp-test-token-0123456789abcdef"


def _start_tcp_daemon(monkeypatch, *, with_token=True):
    if with_token:
        monkeypatch.setenv("GI_CALLER_TOKEN_elena", TOKEN)
    monkeypatch.setenv("HERMES_GREENINVOICE_SOCKET", "tcp://127.0.0.1:0")

    from hermes_greeninvoice import config as _cfg, daemon as _daemon, ratelimit

    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)

    clients = _daemon._ClientHolder(cfg)
    server = _daemon._bind_socket(cfg.socket_path)
    port = server.getsockname()[1]
    stop = threading.Event()
    t = threading.Thread(target=_daemon._accept_loop,
                         args=(server, cfg, clients, stop), daemon=True)
    t.start()
    time.sleep(0.05)

    def teardown():
        stop.set()
        try:
            server.close()
        except OSError:
            pass
        t.join(timeout=2.0)

    return f"tcp://127.0.0.1:{port}", teardown


@pytest.fixture
def tcp_daemon(_isolate_env, monkeypatch):
    addr, teardown = _start_tcp_daemon(monkeypatch)
    monkeypatch.setenv("HERMES_GREENINVOICE_TOKEN", TOKEN)
    yield addr
    teardown()


@pytest.fixture
def tcp_daemon_no_tokens(_isolate_env, monkeypatch):
    addr, teardown = _start_tcp_daemon(monkeypatch, with_token=False)
    yield addr
    teardown()


def test_quota_roundtrip_over_tcp(tcp_daemon):
    resp = client.call("quota", {}, socket_path=tcp_daemon)
    assert resp["ok"] is True
    assert resp["dry_run"] is True


def test_wrong_token_denied(tcp_daemon, monkeypatch):
    monkeypatch.setenv("HERMES_GREENINVOICE_TOKEN", "wrong-token-0123456789abcdef")
    resp = client.call("quota", {}, socket_path=tcp_daemon)
    assert resp["ok"] is False
    assert resp["reason"] == "unknown_caller"


def test_missing_token_denied(tcp_daemon, monkeypatch):
    monkeypatch.delenv("HERMES_GREENINVOICE_TOKEN", raising=False)
    resp = client.call("quota", {}, socket_path=tcp_daemon)
    assert resp["ok"] is False
    assert resp["reason"] == "unknown_caller"


def test_no_tokens_configured_fails_closed(tcp_daemon_no_tokens, monkeypatch):
    # Even a client presenting some token is refused: nothing is configured.
    monkeypatch.setenv("HERMES_GREENINVOICE_TOKEN", TOKEN)
    resp = client.call("quota", {}, socket_path=tcp_daemon_no_tokens)
    assert resp["ok"] is False
    assert resp["reason"] == "tcp_auth_not_configured"


def test_framed_upload_over_tcp(tcp_daemon):
    body = b"%PDF-1.4 fake invoice bytes\n" * 10
    resp = client.call_with_file(
        "upload_expense_file", {"filename": "inv.pdf"}, body,
        socket_path=tcp_daemon)
    # Dry-run: the daemon must consume the framed body and answer coherently
    # (not a protocol/framing error).
    assert resp.get("reason") not in {"invalid_upload_length", "truncated_body",
                                      "body_overrun", "malformed_json"}
    assert resp.get("error") != "protocol"


def test_caller_identity_reaches_ratelimit(tcp_daemon):
    resp = client.call("issue_invoice", {
        "type": 305,
        "client": {"id": "cli_1"},
        "income": [{"description": "Work", "quantity": 1, "price": 1000,
                    "currency": "ILS"}],
        "currency": "ILS",
        "confirm": True,
    }, socket_path=tcp_daemon)
    assert resp["ok"] is True
    assert resp["dry_run"] is True
    assert resp["rate"]["used_hour"] == 1


def test_non_loopback_bind_refused(_isolate_env, monkeypatch):
    monkeypatch.setenv("HERMES_GREENINVOICE_SOCKET", "tcp://0.0.0.0:0")
    from hermes_greeninvoice import config as _cfg, daemon as _daemon
    cfg = _cfg.load_config()
    with pytest.raises(ValueError, match="non-loopback"):
        _daemon._bind_socket(cfg.socket_path)


def test_short_token_rejected_at_config(_isolate_env, monkeypatch):
    monkeypatch.setenv("GI_CALLER_TOKEN_elena", "short")
    from hermes_greeninvoice import config as _cfg
    with pytest.raises(_cfg.ConfigError, match="too short"):
        _cfg.load_config()
