"""Tests for the `contacts` op and the name/alias allowlist fields.

Two layers:
  * allowlist parsing (name/aliases normalization + collision rules), and
  * handle_contacts() building the directory the agent resolves names with,
    including live `remaining_today` and the end-to-end protocol dispatch.
"""

from __future__ import annotations

import json
import socket
import textwrap


# --------------------------------------------------------------------------
# allowlist parsing
# --------------------------------------------------------------------------

def _parse(text: str) -> dict:
    from hermes_mailer import allowlist
    obj = allowlist._parse_yaml_or_json(text)
    return allowlist._normalize_entries(obj)


def test_name_and_aliases_parsed_and_lowercased():
    out = _parse(textwrap.dedent("""
        contacts:
          - email: Yoram@DBExpert.ai
            daily_limit: 5
            name: Yoram
            aliases: ["Yoram", "Co-Founder"]
            note: "co-founder"
    """))
    entry = out["yoram@dbexpert.ai"]
    assert entry["name"] == "Yoram"
    assert entry["aliases"] == ["yoram", "co-founder"]
    assert entry["note"] == "co-founder"


def test_name_and_aliases_default_when_absent():
    out = _parse(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 2
    """))
    assert out["alice@example.com"]["name"] is None
    assert out["alice@example.com"]["aliases"] == []


def test_duplicate_alias_across_contacts_rejected():
    import pytest
    with pytest.raises(ValueError, match="already used by"):
        _parse(textwrap.dedent("""
            contacts:
              - email: a@example.com
                daily_limit: 1
                aliases: ["boss"]
              - email: b@example.com
                daily_limit: 1
                aliases: ["boss"]
        """))


def test_alias_colliding_with_email_rejected():
    import pytest
    with pytest.raises(ValueError, match="collides with a contact email"):
        _parse(textwrap.dedent("""
            contacts:
              - email: a@example.com
                daily_limit: 1
              - email: b@example.com
                daily_limit: 1
                aliases: ["a@example.com"]
        """))


def test_non_string_alias_rejected():
    import pytest
    with pytest.raises(ValueError, match="must be a string"):
        _parse(textwrap.dedent("""
            contacts:
              - email: a@example.com
                daily_limit: 1
                aliases: [123]
        """))


# --------------------------------------------------------------------------
# handle_contacts (in-process)
# --------------------------------------------------------------------------

def _load_cfg(_isolate_env):
    from hermes_mailer import config as _cfg, ratelimit
    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)
    return cfg


def test_contacts_returns_directory(_isolate_env, write_allowlist):
    from hermes_mailer.handler import handle_contacts
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: yoram@dbexpert.ai
            daily_limit: 5
            name: Yoram
            aliases: ["yoram"]
            note: "co-founder"
          - email: alice@example.com
            daily_limit: 2
    """))
    cfg = _load_cfg(_isolate_env)
    resp = handle_contacts(cfg=cfg, caller="elena",
                           request={"request_id": "c1"})
    assert resp["ok"] is True
    assert resp["request_id"] == "c1"
    assert "resets_at" in resp
    # sorted by email -> alice first
    emails = [c["email"] for c in resp["contacts"]]
    assert emails == ["alice@example.com", "yoram@dbexpert.ai"]
    yoram = resp["contacts"][1]
    assert yoram["name"] == "Yoram"
    assert yoram["aliases"] == ["yoram"]
    assert yoram["note"] == "co-founder"
    assert yoram["daily_limit"] == 5
    assert yoram["remaining_today"] == 5


def test_contacts_remaining_today_reflects_sends(_isolate_env, write_allowlist):
    from hermes_mailer.handler import handle_contacts
    from hermes_mailer import ratelimit
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 3
    """))
    cfg = _load_cfg(_isolate_env)
    day = ratelimit.local_day_str(cfg.limit_tz)
    with ratelimit.reserve(
        cfg.ratelimit_db_path, caller="elena", recipient="alice@example.com",
        limit=3, local_day=day, subject_trunc="x", byte_size=0,
        attachment_count=0, request_id="seed", ttl_seconds=600,
    ) as r:
        ratelimit.finalize(cfg.ratelimit_db_path, r, "sent", message_id="m")
    resp = handle_contacts(cfg=cfg, caller="elena",
                           request={"request_id": "c2"})
    assert resp["contacts"][0]["remaining_today"] == 2


def test_contacts_empty_when_no_allowlist_for_caller(_isolate_env, write_allowlist):
    """A caller with no allowlist file gets an empty directory, not an error
    — same non-leaky posture as send's not_in_allowlist."""
    from hermes_mailer.handler import handle_contacts
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: alice@example.com
            daily_limit: 2
    """))
    cfg = _load_cfg(_isolate_env)
    resp = handle_contacts(cfg=cfg, caller="winnow-agent",
                           request={"request_id": "c3"})
    assert resp["ok"] is True
    assert resp["contacts"] == []


# --------------------------------------------------------------------------
# end-to-end protocol dispatch
# --------------------------------------------------------------------------

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


def test_contacts_op_over_socket(daemon_process, write_allowlist):
    write_allowlist(textwrap.dedent("""
        contacts:
          - email: yoram@dbexpert.ai
            daily_limit: 5
            name: Yoram
            aliases: ["yoram"]
    """))
    resp = _rpc(daemon_process, {
        "v": 1, "op": "contacts", "request_id": "p1",
    })
    assert resp["ok"] is True
    assert resp["request_id"] == "p1"
    assert resp["contacts"][0]["email"] == "yoram@dbexpert.ai"
    assert resp["contacts"][0]["aliases"] == ["yoram"]
    assert "v" in resp   # daemon stamps protocol version; client strips it
