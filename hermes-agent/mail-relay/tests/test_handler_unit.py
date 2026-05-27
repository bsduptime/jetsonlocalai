"""Unit tests for the daemon's policy modules.

These exercise handler.handle_send() in-process (no socket) so failure
modes are isolated from the protocol layer.
"""

from __future__ import annotations

import base64
import textwrap


def _call(cfg, caller, **fields):
    from hermes_mailer.handler import handle_send
    fields.setdefault("request_id", "u1")
    return handle_send(cfg=cfg, caller=caller, request=fields)


def test_allowlist_runs_before_attachment_validation(_isolate_env, write_allowlist):
    """Codex F1 (from the plugin's earlier review) still applies in the
    daemon: a non-allowlisted recipient must NOT get attachment-specific
    errors that would let the tool be a file-existence oracle."""
    from hermes_mailer import config as _cfg
    write_allowlist("contacts: []\n")
    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    from hermes_mailer import ratelimit
    ratelimit.init_schema(cfg.ratelimit_db_path)
    resp = _call(cfg, "elena",
                 to="evil@example.com", subject="x", body="y",
                 attachments=[{"filename": "evil.xyz",
                               "content_b64": base64.b64encode(b"x").decode("ascii")}])
    assert resp["error"] == "not_allowed"
    assert resp["reason"] == "not_in_allowlist"


def test_unknown_caller_returns_not_in_allowlist(_isolate_env, write_allowlist):
    """A request from an unknown caller (in this test, we pass
    'winnow-agent' which has no allowlist file) should get the same
    not_in_allowlist error — not leak whether the caller exists."""
    from hermes_mailer import config as _cfg
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """))
    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    from hermes_mailer import ratelimit
    ratelimit.init_schema(cfg.ratelimit_db_path)
    # Caller "winnow-agent" has NO allowlist file; the elena single-file
    # form is not used for non-elena callers.
    resp = _call(cfg, "winnow-agent",
                 to="alice@example.com", subject="x", body="y")
    assert resp["error"] == "not_allowed"
    assert resp["reason"] == "not_in_allowlist"


def test_per_caller_rate_limit_isolation(_isolate_env):
    """Elena hitting her cap doesn't affect a future winnow-agent's
    quota for the same recipient."""
    from hermes_mailer import config as _cfg, ratelimit
    # Multi-file allowlist form with two callers
    cfg_dir = _isolate_env["cfg_dir"]
    (cfg_dir / "allowlists").mkdir()
    (cfg_dir / "allowlists" / "elena.yaml").write_text(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 1
    """), encoding="utf-8")
    (cfg_dir / "allowlists" / "winnow-agent.yaml").write_text(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 1
    """), encoding="utf-8")

    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)

    # Burn elena's quota by writing a sent row directly
    day = ratelimit.local_day_str(cfg.limit_tz)
    with ratelimit.reserve(
        cfg.ratelimit_db_path, caller="elena", recipient="alice@example.com",
        limit=1, local_day=day, subject_trunc="x", byte_size=0,
        attachment_count=0, request_id="seed", ttl_seconds=600,
    ) as r:
        ratelimit.finalize(cfg.ratelimit_db_path, r, "sent", message_id="m")

    # Elena is now over
    resp_e = _call(cfg, "elena", to="alice@example.com", subject="x", body="y")
    assert resp_e["error"] == "not_allowed"
    assert resp_e["reason"] == "rate_limit_exceeded"

    # winnow-agent still has the full quota
    resp_w = _call(cfg, "winnow-agent",
                   to="alice@example.com", subject="x", body="y")
    assert resp_w["ok"] is True


def test_resend_path_passes_base64_attachment(_isolate_env, write_allowlist,
                                              fake_resend, monkeypatch,
                                              fixtures_dir):
    """When transport is resend and dry_run is false, the daemon must
    encode attachments as base64 (not list-of-int) — same fix as the
    original plugin's Codex F6."""
    from hermes_mailer import config as _cfg, ratelimit
    monkeypatch.setenv("EMAIL_DRY_RUN", "false")
    monkeypatch.setenv("EMAIL_TRANSPORT", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_xxx")
    monkeypatch.setenv("EMAIL_FROM", "Test <test@example.com>")
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """))
    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)
    pdf_bytes = (fixtures_dir / "tiny.pdf").read_bytes()
    resp = _call(cfg, "elena",
                 to="alice@example.com", subject="hi", body="hi",
                 attachments=[{"filename": "real.pdf",
                               "content_b64": base64.b64encode(pdf_bytes).decode("ascii")}])
    assert resp["ok"] is True
    assert resp["status"] == "sent"
    # Confirm the fake Resend saw a base64 STRING, not a list-of-ints.
    assert len(fake_resend) == 1
    att = fake_resend[0]["attachments"][0]
    assert isinstance(att["content"], str)
    assert base64.b64decode(att["content"]) == pdf_bytes


def test_dry_run_belt_wins(_isolate_env, write_allowlist, monkeypatch,
                            fake_resend):
    """EMAIL_DRY_RUN=true MUST override EMAIL_TRANSPORT=resend so a
    misconfig can't accidentally hit Resend."""
    from hermes_mailer import config as _cfg, ratelimit
    monkeypatch.setenv("EMAIL_TRANSPORT", "resend")
    monkeypatch.setenv("EMAIL_DRY_RUN", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_xxx")
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """))
    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)
    resp = _call(cfg, "elena",
                 to="alice@example.com", subject="hi", body="hi")
    assert resp["ok"] is True
    assert resp["status"] == "dry_run"
    assert len(fake_resend) == 0    # MUST NOT have called the fake Resend
