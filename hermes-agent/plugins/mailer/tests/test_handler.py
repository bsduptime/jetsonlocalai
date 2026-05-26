from __future__ import annotations

import json
import textwrap

import pytest

from hermes_email_pkg import allowlist as _allowlist_mod
from hermes_email_pkg import handler


def _write_allowlist(pdir, body):
    p = pdir / "allowlist.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_allowlist_cache():
    _allowlist_mod._reset_cache_for_tests()
    yield
    _allowlist_mod._reset_cache_for_tests()


def call(args):
    return json.loads(handler.send_email(args))


def test_send_dry_run_success(_isolate_env):
    pdir = _isolate_env
    _write_allowlist(pdir, """
        contacts:
          - email: alice@example.com
            daily_limit: 2
    """)
    resp = call({"to": "alice@example.com", "subject": "hi", "body": "hello"})
    assert resp["ok"] is True
    assert resp["status"] == "dry_run"
    assert resp["to"] == "alice@example.com"
    assert resp["remaining_today"] == 2          # dry_run doesn't count
    assert resp["limit"] == 2
    assert "T00:00:00" in resp["resets_at"]


def test_send_not_in_allowlist(_isolate_env):
    _write_allowlist(_isolate_env, "contacts: []\n")
    resp = call({"to": "evil@example.com", "subject": "hi", "body": "hello"})
    assert resp["ok"] is False
    assert resp["error"] == "not_allowed"
    assert resp["reason"] == "not_in_allowlist"


def test_send_with_empty_allowlist_file(_isolate_env):
    _write_allowlist(_isolate_env, "")
    resp = call({"to": "anyone@example.com", "subject": "hi", "body": "hello"})
    assert resp["reason"] == "not_in_allowlist"


def test_send_with_missing_allowlist_file(_isolate_env):
    # Don't create the file at all
    resp = call({"to": "anyone@example.com", "subject": "hi", "body": "hello"})
    assert resp["reason"] == "not_in_allowlist"


def test_send_rate_limit_exceeded(_isolate_env, monkeypatch):
    pdir = _isolate_env
    monkeypatch.setenv("EMAIL_DRY_RUN", "false")
    monkeypatch.setenv("EMAIL_TRANSPORT", "dry_run")
    _write_allowlist(pdir, """
        contacts:
          - email: alice@example.com
            daily_limit: 2
    """)
    # With EMAIL_DRY_RUN=false but EMAIL_TRANSPORT=dry_run, sends count as 'sent'.
    # Hmm — actually transport.name=='dry_run' makes us finalize as dry_run. So
    # to actually charge the quota in tests we must use a fake transport that
    # finalizes as 'sent'. Easier: write rows directly via ratelimit module.
    from hermes_email_pkg import ratelimit
    db = pdir / "state" / "ratelimit.db"
    ratelimit.init_schema(db)
    day = ratelimit.local_day_str("local")
    for _ in range(2):
        with ratelimit.reserve(db, recipient="alice@example.com", limit=2,
                               local_day=day, subject_trunc="x", byte_size=0,
                               attachment_count=0, ttl_seconds=180) as r:
            ratelimit.finalize(db, r, "sent", message_id="m")
    resp = call({"to": "alice@example.com", "subject": "hi", "body": "hello"})
    assert resp["ok"] is False
    assert resp["reason"] == "rate_limit_exceeded"
    assert resp["sent_today"] == 2
    assert resp["limit"] == 2


def test_header_injection_in_to_is_invalid_input(_isolate_env):
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    resp = call({
        "to": "alice@example.com\r\nBcc: attacker@evil",
        "subject": "hi", "body": "hello",
    })
    assert resp["error"] == "invalid_input"
    assert resp["reason"] in {"header_injection", "invalid_email"}


def test_missing_subject_is_invalid_input(_isolate_env):
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    resp = call({"to": "alice@example.com", "body": "hello"})
    assert resp["error"] == "invalid_input"


def test_subject_too_long_is_invalid_input(_isolate_env):
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    resp = call({"to": "alice@example.com", "subject": "x" * 201, "body": "h"})
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "subject_too_long"


def test_attachment_path_outside_prefix(_isolate_env, fixtures_dir, monkeypatch):
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    # The fixtures live in the plugin source dir, NOT under /tmp or tmp_path.
    monkeypatch.setenv("EMAIL_ATTACHMENT_ALLOWED_PREFIXES", "/tmp/")
    resp = call({
        "to": "alice@example.com", "subject": "hi", "body": "hello",
        "attachments": [str(fixtures_dir / "tiny.pdf")],
    })
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "attachment_outside_allowed_prefixes"


def test_attachment_dry_run_success(_isolate_env, stage_fixture):
    pdir = _isolate_env
    _write_allowlist(pdir, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    attach_path = stage_fixture("tiny.pdf")
    resp = call({
        "to": "alice@example.com", "subject": "hi", "body": "hello",
        "attachments": [str(attach_path)],
    })
    assert resp["ok"] is True
    # The .eml dump should exist
    dryrun_dir = pdir / "state" / "dryrun"
    files = list(dryrun_dir.iterdir())
    assert len(files) == 1


def test_response_is_valid_json_string(_isolate_env):
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    out = handler.send_email({"to": "alice@example.com",
                              "subject": "hi", "body": "h"})
    # Must be parseable JSON, not a dict or anything else
    assert isinstance(out, str)
    json.loads(out)


def test_allowlist_runs_before_attachment_validation(_isolate_env):
    """Codex F1: a non-allowlisted recipient must NOT get attachment-specific
    error messages — that would make the tool a file-existence/type oracle."""
    _write_allowlist(_isolate_env, "contacts: []\n")
    resp = call({
        "to": "evil@example.com", "subject": "hi", "body": "hello",
        "attachments": ["/tmp/this-file-very-likely-does-not-exist-xyzzy.pdf"],
    })
    assert resp["error"] == "not_allowed"
    assert resp["reason"] == "not_in_allowlist"


def test_non_string_body_is_invalid_input(_isolate_env):
    """Codex F5: non-string field values must return invalid_input."""
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    resp = call({"to": "alice@example.com", "subject": "hi", "body": 12345})
    assert resp["error"] == "invalid_input"
    assert resp["reason"] in {"invalid_field_type", "missing_field"}


def test_csv_with_binary_tail_rejected(_isolate_env, tmp_path):
    """Codex F2: a CSV with a benign UTF-8 prefix and binary tail must be
    rejected. The validator now checks the full file, not just first 8 KiB."""
    bad_csv = tmp_path / "evil.csv"
    bad_csv.write_bytes(b"name,age\n" + b"a" * 9000 + b"\x00\xff\xfe\x01" * 100)
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    resp = call({
        "to": "alice@example.com", "subject": "hi", "body": "h",
        "attachments": [str(bad_csv)],
    })
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "attachment_magic_mismatch"


def test_dry_run_wins_even_with_resend_transport(_isolate_env, monkeypatch):
    """Belt-and-suspenders: dry_run=true forces DryRunTransport regardless
    of EMAIL_TRANSPORT — so a misconfig can't accidentally call Resend."""
    monkeypatch.setenv("EMAIL_TRANSPORT", "resend")
    monkeypatch.setenv("EMAIL_DRY_RUN", "true")
    monkeypatch.setenv("RESEND_API_KEY", "rk_test")
    # Spy on resend to make sure we DON'T hit it
    import sys, types
    fake = types.ModuleType("resend")

    class _Emails:
        @staticmethod
        def send(params):
            raise AssertionError("resend should never be called in dry-run")

    fake.Emails = _Emails
    monkeypatch.setitem(sys.modules, "resend", fake)
    _write_allowlist(_isolate_env, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    resp = call({"to": "alice@example.com", "subject": "hi", "body": "h"})
    assert resp["ok"] is True
    assert resp["status"] == "dry_run"
