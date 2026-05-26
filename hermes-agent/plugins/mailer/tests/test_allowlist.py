from __future__ import annotations

import textwrap

import pytest

from hermes_email_pkg import allowlist


@pytest.fixture(autouse=True)
def _reset_cache():
    allowlist._reset_cache_for_tests()
    yield
    allowlist._reset_cache_for_tests()


def write_yaml(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_loads_valid_yaml(tmp_path):
    p = tmp_path / "allow.yaml"
    write_yaml(p, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
            note: friend
          - email: BOB@Example.com
            daily_limit: 2
    """)
    contacts, err = allowlist.load_allowlist(p)
    assert err is None
    assert "alice@example.com" in contacts
    assert "bob@example.com" in contacts          # case-normalized to lower
    assert contacts["alice@example.com"]["daily_limit"] == 5


def test_lookup_is_case_insensitive(tmp_path):
    p = tmp_path / "allow.yaml"
    write_yaml(p, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    contacts, _ = allowlist.load_allowlist(p)
    assert allowlist.lookup("ALICE@example.COM", contacts) is not None
    assert allowlist.lookup("nobody@example.com", contacts) is None


def test_missing_file_is_empty_no_error_str(tmp_path):
    p = tmp_path / "missing.yaml"
    contacts, err = allowlist.load_allowlist(p)
    assert contacts == {}
    assert "missing" in (err or "")


def test_empty_contacts_list(tmp_path):
    p = tmp_path / "allow.yaml"
    write_yaml(p, "contacts: []\n")
    contacts, err = allowlist.load_allowlist(p)
    assert contacts == {}
    assert err is None


def test_parse_error_falls_back_to_cache(tmp_path):
    p = tmp_path / "allow.yaml"
    write_yaml(p, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
    """)
    contacts, err = allowlist.load_allowlist(p)
    assert err is None
    assert "alice@example.com" in contacts
    # now corrupt the file
    p.write_text(": : ::nopenope:", encoding="utf-8")
    contacts2, err2 = allowlist.load_allowlist(p)
    assert "alice@example.com" in contacts2
    assert err2 and err2.startswith("parse_failed_using_cache")


def test_no_cache_on_first_failure_returns_empty(tmp_path):
    p = tmp_path / "allow.yaml"
    p.write_text("contacts: not-a-list\n", encoding="utf-8")
    contacts, err = allowlist.load_allowlist(p)
    assert contacts == {}
    assert err and err.startswith("parse_failed")


def test_invalid_daily_limit_rejected(tmp_path):
    p = tmp_path / "allow.yaml"
    write_yaml(p, """
        contacts:
          - email: alice@example.com
            daily_limit: 0
    """)
    contacts, err = allowlist.load_allowlist(p)
    assert contacts == {}
    assert "out of range" in (err or "")


def test_duplicate_email_rejected(tmp_path):
    p = tmp_path / "allow.yaml"
    write_yaml(p, """
        contacts:
          - email: alice@example.com
            daily_limit: 5
          - email: ALICE@example.com
            daily_limit: 3
    """)
    contacts, err = allowlist.load_allowlist(p)
    assert contacts == {}
    assert "duplicate" in (err or "")


def test_invalid_email_rejected(tmp_path):
    p = tmp_path / "allow.yaml"
    write_yaml(p, """
        contacts:
          - email: not-an-email
            daily_limit: 1
    """)
    contacts, err = allowlist.load_allowlist(p)
    assert contacts == {}
    assert "not a valid address" in (err or "")
