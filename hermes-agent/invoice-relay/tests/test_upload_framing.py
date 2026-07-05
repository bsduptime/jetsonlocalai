"""End-to-end framed upload over the real UDS: header line + raw body, through
the daemon's read path (dry-run, so no upstream)."""

from __future__ import annotations

from hermes_greeninvoice_client import call, call_with_file


def test_framed_upload_roundtrip(daemon_process):
    resp = call_with_file(
        "upload_expense_file",
        {"filename": "receipt.pdf", "content_type": "application/pdf"},
        b"%PDF-1.4 hello world payload",
        socket_path=daemon_process,
    )
    assert resp["ok"] is True
    assert resp["dry_run"] is True
    assert resp["op"] == "upload_expense_file"


def test_framed_upload_empty_body_rejected(daemon_process):
    # byte_len 0 is an invalid upload length (protocol error), not a crash.
    resp = call_with_file(
        "upload_expense_file",
        {"filename": "receipt.pdf", "content_type": "application/pdf"},
        b"",
        socket_path=daemon_process,
    )
    assert resp["ok"] is False
    assert resp["reason"] == "invalid_upload_length"


def test_normal_call_still_works(daemon_process):
    # A plain JSON op over the same daemon is unaffected by the framing path.
    resp = call("quota", {}, socket_path=daemon_process)
    assert resp["ok"] is True
    assert "expense_write" in resp["quotas"]
