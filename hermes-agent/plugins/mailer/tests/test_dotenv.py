from __future__ import annotations

import os

from hermes_email_pkg import dotenv


def test_loads_simple_key_value(tmp_path, monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    dotenv.load_dotenv(p)
    assert os.environ["FOO"] == "bar"


def test_does_not_overwrite_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO", "from-env")
    p = tmp_path / ".env"
    p.write_text("FOO=from-file\n")
    dotenv.load_dotenv(p)
    assert os.environ["FOO"] == "from-env"


def test_supports_export_and_comments(tmp_path, monkeypatch):
    for k in ("A", "B"):
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text("# comment\nexport A=one\nB = two  # trailing\n")
    dotenv.load_dotenv(p)
    assert os.environ["A"] == "one"
    assert os.environ["B"] == "two"


def test_double_quoted_value_with_escape(tmp_path, monkeypatch):
    monkeypatch.delenv("MULTI", raising=False)
    p = tmp_path / ".env"
    p.write_text(r'MULTI="line1\nline2"' + "\n")
    dotenv.load_dotenv(p)
    assert os.environ["MULTI"] == "line1\nline2"


def test_single_quoted_value_is_literal(tmp_path, monkeypatch):
    monkeypatch.delenv("LIT", raising=False)
    p = tmp_path / ".env"
    p.write_text(r"LIT='no\nescape'" + "\n")
    dotenv.load_dotenv(p)
    assert os.environ["LIT"] == r"no\nescape"


def test_missing_file_returns_empty(tmp_path):
    assert dotenv.load_dotenv(tmp_path / "nope") == {}
