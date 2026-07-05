"""Upload file-path allowlist: only regular files inside an allowed dir may be
read, and no symlink/.. escape."""

from __future__ import annotations

import json
import os

import pytest

from hermes_gi_pkg import handler


def test_reads_allowlisted_file(_isolate_env):
    f = _isolate_env / "media" / "receipt.pdf"
    f.write_bytes(b"%PDF fake data")
    fn, ct, data = handler._read_upload_file(str(f))
    assert fn == "receipt.pdf"
    assert ct == "application/pdf"
    assert data == b"%PDF fake data"


def test_rejects_outside_allowlist(_isolate_env):
    f = _isolate_env / "secret.pdf"       # outside media/
    f.write_bytes(b"x")
    with pytest.raises(PermissionError):
        handler._read_upload_file(str(f))


def test_rejects_symlink_escape(_isolate_env):
    target = _isolate_env / "outside.pdf"
    target.write_bytes(b"secret")
    link = _isolate_env / "media" / "link.pdf"
    os.symlink(target, link)
    with pytest.raises(PermissionError):
        handler._read_upload_file(str(link))


def test_rejects_traversal(_isolate_env):
    (_isolate_env / "outside.pdf").write_bytes(b"x")
    p = str(_isolate_env / "media" / ".." / "outside.pdf")
    with pytest.raises(PermissionError):
        handler._read_upload_file(p)


def test_rejects_unsupported_ext(_isolate_env):
    f = _isolate_env / "media" / "x.exe"
    f.write_bytes(b"MZ")
    with pytest.raises(ValueError):
        handler._read_upload_file(str(f))


def test_rejects_oversize(_isolate_env, monkeypatch):
    monkeypatch.setattr(handler, "_MAX_UPLOAD_BYTES", 4)
    f = _isolate_env / "media" / "big.pdf"
    f.write_bytes(b"12345")
    with pytest.raises(ValueError):
        handler._read_upload_file(str(f))


def test_upload_handler_rejects_bad_path_json(_isolate_env):
    out = json.loads(handler.gi_upload_expense_file({"path": "/etc/passwd"}))
    assert out["ok"] is False
    assert out["reason"] in ("file_rejected", "file_not_found")


def test_upload_handler_daemon_unreachable_json(_isolate_env):
    f = _isolate_env / "media" / "r.pdf"
    f.write_bytes(b"%PDF")
    out = json.loads(handler.gi_upload_expense_file({"path": str(f)}))
    assert out["ok"] is False
    assert out["reason"] == "daemon_unreachable"
