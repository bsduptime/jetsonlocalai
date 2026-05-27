"""End-to-end protocol tests: connect to a real daemon over UDS, send a
JSON envelope, parse the JSON response.

Daemon runs in-process via the `daemon_process` fixture. We connect as
the same UID (mapped to "elena" via env), so caller resolution works.
"""

from __future__ import annotations

import json
import socket
import textwrap


def _rpc(socket_path: str, req: dict, *, timeout: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(socket_path)
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = bytearray()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                break
        return json.loads(bytes(buf).split(b"\n", 1)[0])
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


def test_send_dry_run_success(daemon_process, write_allowlist):
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 3
    """))
    resp = _rpc(daemon_process, {
        "v": 1, "op": "send", "request_id": "t1",
        "to": "alice@example.com",
        "subject": "hi",
        "body": "hello",
    })
    assert resp["ok"] is True
    assert resp["status"] == "dry_run"
    assert resp["to"] == "alice@example.com"
    assert resp["request_id"] == "t1"
    assert resp["limit"] == 3
    assert resp["remaining_today"] == 3   # dry_run doesn't count
    assert "resets_at" in resp


def test_not_in_allowlist(daemon_process, write_allowlist):
    write_allowlist("contacts: []\n")
    resp = _rpc(daemon_process, {
        "v": 1, "op": "send", "request_id": "t2",
        "to": "evil@example.com", "subject": "hi", "body": "hi",
    })
    assert resp["ok"] is False
    assert resp["error"] == "not_allowed"
    assert resp["reason"] == "not_in_allowlist"


def test_version_mismatch_rejected(daemon_process):
    resp = _rpc(daemon_process, {
        "v": 99, "op": "send", "request_id": "t3",
        "to": "alice@example.com", "subject": "hi", "body": "hi",
    })
    assert resp["ok"] is False
    assert resp["error"] == "protocol"
    assert resp["reason"] == "version_mismatch"


def test_unknown_op_rejected(daemon_process):
    resp = _rpc(daemon_process, {
        "v": 1, "op": "nuke", "request_id": "t4",
    })
    assert resp["ok"] is False
    assert resp["error"] == "protocol"
    assert resp["reason"] == "unknown_op"


def test_malformed_json_rejected(daemon_process):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(daemon_process)
    s.sendall(b"not even close to json\n")
    buf = s.recv(4096)
    resp = json.loads(buf.split(b"\n", 1)[0])
    assert resp["error"] == "protocol"
    assert resp["reason"] == "malformed_json"
    s.close()


def test_header_injection_in_to_rejected(daemon_process, write_allowlist):
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """))
    resp = _rpc(daemon_process, {
        "v": 1, "op": "send", "request_id": "t5",
        "to": "alice@example.com\r\nBcc: attacker@evil",
        "subject": "hi", "body": "hi",
    })
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert resp["reason"] in {"header_injection", "invalid_email"}


def test_attachment_magic_mismatch_rejected(daemon_process, write_allowlist):
    """A LYING client sends bytes that aren't a real PDF but claims a
    .pdf filename. Daemon's magic-byte check catches it."""
    import base64
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """))
    resp = _rpc(daemon_process, {
        "v": 1, "op": "send", "request_id": "t6",
        "to": "alice@example.com", "subject": "hi", "body": "hi",
        "attachments": [
            {"filename": "evil.pdf",
             "content_b64": base64.b64encode(b"not a real pdf").decode("ascii")},
        ],
    })
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "attachment_magic_mismatch"


def test_attachment_real_pdf_passes(daemon_process, write_allowlist, fixtures_dir):
    import base64
    pdf_bytes = (fixtures_dir / "tiny.pdf").read_bytes()
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """))
    resp = _rpc(daemon_process, {
        "v": 1, "op": "send", "request_id": "t7",
        "to": "alice@example.com", "subject": "hi", "body": "hi",
        "attachments": [
            {"filename": "real.pdf",
             "content_b64": base64.b64encode(pdf_bytes).decode("ascii")},
        ],
    })
    assert resp["ok"] is True


def test_attachment_basename_strips_path_components(daemon_process, write_allowlist, fixtures_dir):
    """A lying client passes filename='/etc/hermes-mailer/.env' — we
    strip the path components and only honor the basename ('.env' which
    has no allowed extension so it's rejected). Defense in depth."""
    import base64
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """))
    resp = _rpc(daemon_process, {
        "v": 1, "op": "send", "request_id": "t8",
        "to": "alice@example.com", "subject": "hi", "body": "hi",
        "attachments": [
            {"filename": "/etc/hermes-mailer/.env",
             "content_b64": base64.b64encode(b"%PDF-1.4\nfake").decode("ascii")},
        ],
    })
    assert resp["ok"] is False
    # filename had a slash → bad_basename rejection
    assert resp["reason"] in {
        "attachment_bad_basename",
        "attachment_extension_not_allowed",
    }


def test_rate_limit_exceeded(daemon_process, write_allowlist):
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 2
    """))
    # Make 2 SENT-counted requests by switching to a real "sent"
    # transport. We use the fake resend module via env.
    # Simpler approach: write reservations directly via the DB path,
    # then attempt one more via RPC and confirm rate-limit response.
    from hermes_mailer import ratelimit, config as _cfg
    cfg = _cfg.load_config()
    ratelimit.init_schema(cfg.ratelimit_db_path)
    day = ratelimit.local_day_str(cfg.limit_tz)
    for _ in range(2):
        with ratelimit.reserve(
            cfg.ratelimit_db_path,
            caller="elena", recipient="alice@example.com", limit=2,
            local_day=day, subject_trunc="x", byte_size=0,
            attachment_count=0, request_id="seed",
            ttl_seconds=600,
        ) as r:
            ratelimit.finalize(cfg.ratelimit_db_path, r, "sent", message_id="m")
    resp = _rpc(daemon_process, {
        "v": 1, "op": "send", "request_id": "t9",
        "to": "alice@example.com", "subject": "hi", "body": "hi",
    })
    assert resp["ok"] is False
    assert resp["error"] == "not_allowed"
    assert resp["reason"] == "rate_limit_exceeded"
    assert resp["sent_today"] == 2
    assert resp["limit"] == 2


def test_slow_drip_client_hits_wallclock_deadline():
    """Codex code-review F#3: a client that trickles bytes slowly must
    NOT be able to hold a worker slot indefinitely (per-recv timeout
    doesn't enforce a wall-clock cap). With the monotonic deadline,
    the read raises after deadline_seconds regardless of arrival rate."""
    import socket as _socket
    import threading
    import time

    from hermes_mailer.daemon import _read_until_newline
    from hermes_mailer.errors import ProtocolError

    s1, s2 = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)

    def _drip():
        # Send one byte at a time, never reaching a newline, slow enough
        # that each individual recv succeeds (resetting any per-recv
        # timeout) but the total exceeds our deadline.
        try:
            for _ in range(100):
                s1.sendall(b"X")
                time.sleep(0.15)
        except OSError:
            pass

    sender = threading.Thread(target=_drip, daemon=True)
    sender.start()
    try:
        s2.settimeout(10.0)
        import pytest
        start = time.monotonic()
        with pytest.raises(ProtocolError) as ei:
            # Deadline of 0.4s — short enough for the test to be fast.
            _read_until_newline(s2, max_bytes=4096, deadline_seconds=0.4)
        elapsed = time.monotonic() - start
        assert ei.value.reason == "read_timeout"
        assert elapsed < 1.5  # didn't hang indefinitely
    finally:
        try: s1.close()
        except OSError: pass
        try: s2.close()
        except OSError: pass


def test_oversized_request_rejected_unit():
    """Unit-test the size cap directly on _read_until_newline so we don't
    have to race the kernel's socket buffer with a 30 MiB sendall."""
    import socket as _socket

    from hermes_mailer.daemon import _read_until_newline
    from hermes_mailer.errors import ProtocolError

    s1, s2 = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s2.settimeout(2.0)
        # 200 bytes, no newline, with max=100. Should raise.
        s1.sendall(b"X" * 200)
        s1.close()
        import pytest
        with pytest.raises(ProtocolError) as ei:
            _read_until_newline(s2, max_bytes=100)
        assert ei.value.reason == "request_too_large"
    finally:
        try: s2.close()
        except OSError: pass
