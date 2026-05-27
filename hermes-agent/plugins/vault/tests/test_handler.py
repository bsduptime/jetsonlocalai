"""End-to-end tests of the vault plugin handlers.

After Codex review, we cover:
  - happy paths for all 5 tools
  - symlink attacks (target swap, parent swap, tmp file pre-place)
  - frontmatter edge cases (CRLF, BOM, comments, ISO datetime, unparseable)
  - size cap on reads, conflict-file read refusal
  - bounded scan
  - clock skew (future last_compiled)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

import pytest


def call(handler, name, **args):
    fn = getattr(handler, name)
    return json.loads(fn(args))


# ---------- vault_session_brief ----------

def test_session_brief_returns_index_and_schedule(vault, handler):
    res = call(handler, "vault_session_brief")
    assert res["ok"] is True
    assert res["index"]["path"] == "INDEX.md"
    assert res["schedule"]["path"] == "areas/schedule.md"
    assert res["daily"] == []
    assert res["conflicts"] == []
    assert res["blocking"] is False


def test_session_brief_includes_daily_within_window(vault, handler):
    today = _dt.date.today()
    (vault / "daily" / f"{today.isoformat()}.md").write_text("# today\n", encoding="utf-8")
    old = today - _dt.timedelta(days=30)
    (vault / "daily" / f"{old.isoformat()}.md").write_text("# old\n", encoding="utf-8")
    res = call(handler, "vault_session_brief", days=7)
    daily_dates = [d["date"] for d in res["daily"]]
    assert today.isoformat() in daily_dates
    assert old.isoformat() not in daily_dates


def test_session_brief_flags_conflicts_and_sets_blocking(vault, handler):
    (vault / "agents" / "hermes" / "observations" / "x.sync-conflict-20260101-120000-AAA.md").write_text(
        "conflict", encoding="utf-8"
    )
    res = call(handler, "vault_session_brief")
    assert len(res["conflicts"]) == 1
    assert res["blocking"] is True
    assert any("sync-conflict" in w for w in res["warnings"])


def test_session_brief_flags_stale_index(vault, handler):
    (vault / "INDEX.md").write_text(
        "---\nlast_compiled: 2020-01-01\n---\n# old\n", encoding="utf-8"
    )
    res = call(handler, "vault_session_brief")
    assert res["index"]["staleness"]["state"] == "stale"
    assert any("INDEX.md" in w for w in res["warnings"])


def test_session_brief_flags_unknown_freshness(vault, handler):
    # No last_compiled in frontmatter at all
    (vault / "INDEX.md").write_text("# no frontmatter\n", encoding="utf-8")
    res = call(handler, "vault_session_brief")
    assert res["index"]["staleness"]["state"] == "unknown"
    assert any("INDEX.md" in w for w in res["warnings"])


def test_session_brief_skips_symlinked_daily(vault, handler):
    today = _dt.date.today()
    # symlink in daily/ pointing outside vault
    outside = vault.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    sym = vault / "daily" / f"{today.isoformat()}.md"
    os.symlink(outside, sym)
    res = call(handler, "vault_session_brief", days=7)
    # The symlinked entry should be skipped silently — handler must not leak outside content.
    leaked = [d for d in res["daily"] if "outside" in d.get("content", "")]
    assert leaked == []


def test_session_brief_rejects_out_of_range_days(vault, handler):
    res = call(handler, "vault_session_brief", days=99)
    assert res["ok"] is False
    assert res["reason"] == "days_out_of_range"


# ---------- vault_read ----------

def test_read_returns_parsed_frontmatter(vault, handler):
    p = vault / "areas" / "customers.md"
    p.write_text(
        "---\nlast_compiled: 2026-05-20\nstaleness_warning_after_days: 30\n---\n# customers\n",
        encoding="utf-8",
    )
    res = call(handler, "vault_read", path="areas/customers.md")
    assert res["ok"] is True
    assert res["frontmatter"]["last_compiled"] == "2026-05-20"
    assert res["staleness_threshold_days"] == 30


def test_read_rejects_path_traversal(vault, handler):
    res = call(handler, "vault_read", path="../etc/passwd")
    assert res["ok"] is False
    assert res["reason"] == "path_unsafe"


def test_read_rejects_absolute_path(vault, handler):
    res = call(handler, "vault_read", path="/etc/passwd")
    assert res["ok"] is False
    assert res["reason"] == "path_unsafe"


def test_read_rejects_symlink_escaping_vault(vault, handler):
    """A symlink whose target is outside the vault must be rejected.
    resolve_under() catches this first (path_unsafe), before is_symlink does."""
    outside = vault.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    sym = vault / "areas" / "leak.md"
    os.symlink(outside, sym)
    res = call(handler, "vault_read", path="areas/leak.md")
    assert res["ok"] is False
    assert res["reason"] in ("path_unsafe", "is_symlink")


def test_read_rejects_inside_vault_symlink(vault, handler):
    """A symlink whose target IS inside the vault must still be rejected — we
    don't want the LLM to be tricked into reading aliased content."""
    sym = vault / "areas" / "alias.md"
    os.symlink(vault / "INDEX.md", sym)
    res = call(handler, "vault_read", path="areas/alias.md")
    assert res["ok"] is False
    assert res["reason"] == "is_symlink"


def test_read_404_on_missing(vault, handler):
    res = call(handler, "vault_read", path="does/not/exist.md")
    assert res["ok"] is False
    assert res["reason"] == "file_missing"


def test_read_rejects_directory(vault, handler):
    res = call(handler, "vault_read", path="areas")
    assert res["ok"] is False
    assert res["reason"] == "not_a_file"


def test_read_refuses_conflict_file(vault, handler):
    (vault / "areas" / "a.sync-conflict-20260101-000000-AAA.md").write_text("oops", encoding="utf-8")
    res = call(handler, "vault_read", path="areas/a.sync-conflict-20260101-000000-AAA.md")
    assert res["ok"] is False
    assert res["reason"] == "sync_conflict_file"


def test_read_enforces_size_cap(vault, handler, monkeypatch):
    from hermes_vault_pkg import handler as h
    monkeypatch.setattr(h, "MAX_READ_BYTES", 16)
    p = vault / "areas" / "big.md"
    p.write_text("X" * 1024, encoding="utf-8")
    res = call(handler, "vault_read", path="areas/big.md")
    assert res["ok"] is False
    assert res["error"] == "read_failed"


# ---------- vault_write_observation ----------

def test_write_observation_creates_timestamped_file(vault, handler):
    res = call(
        handler,
        "vault_write_observation",
        slug="david-prefers-dark-mode",
        body="David said today he prefers dark mode in apps.",
    )
    assert res["ok"] is True
    p = vault / res["path"]
    assert p.exists()
    txt = p.read_text(encoding="utf-8")
    assert "slug: david-prefers-dark-mode" in txt
    assert "David said today" in txt


def test_write_observation_rejects_bad_slug(vault, handler):
    for bad in ["UPPER", "with space", "trailing-", "-leading", "", "a" * 100, "path/sep"]:
        res = call(handler, "vault_write_observation", slug=bad, body="x")
        assert res["ok"] is False, f"slug {bad!r} should have been rejected"
        assert res["reason"] in ("slug_invalid",)


def test_write_observation_rejects_empty_body(vault, handler):
    res = call(handler, "vault_write_observation", slug="x", body="  \n  ")
    assert res["ok"] is False
    assert res["reason"] == "body_required"


def test_write_observation_refuses_pre_placed_symlink(vault, handler):
    # Pre-place a symlink at where the next observation file would land —
    # but we don't know the timestamp, so place one with a fixed slug and
    # ensure the handler refuses to write into it.
    obs_dir = vault / "agents" / "hermes" / "observations"
    outside = vault.parent / "outside.md"
    outside.write_text("original", encoding="utf-8")
    # We can't predict the timestamp; instead, exercise the parent-dir
    # symlink check by replacing the observations dir with a symlink.
    import shutil
    shutil.rmtree(obs_dir)
    os.symlink(vault.parent / "outside_dir", obs_dir)
    (vault.parent / "outside_dir").mkdir()
    res = call(handler, "vault_write_observation", slug="a", body="b")
    assert res["ok"] is False
    assert res["reason"] in ("observations_dir_is_symlink", "path_unsafe")


def test_write_observation_collision_returns_filename_exists(vault, handler, monkeypatch):
    # Pin the clock so both calls land on the same timestamp.
    import datetime as dt
    real_now = dt.datetime.now()
    pinned = real_now.replace(microsecond=0)

    class FrozenDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned

    from hermes_vault_pkg import handler as h
    monkeypatch.setattr(h._dt, "datetime", FrozenDT)
    r1 = call(handler, "vault_write_observation", slug="dup", body="first")
    r2 = call(handler, "vault_write_observation", slug="dup", body="second")
    assert r1["ok"] is True
    assert r2["ok"] is False
    assert r2["reason"] == "filename_exists"


# ---------- vault_write_memory ----------

def test_write_memory_creates_and_overwrites(vault, handler):
    res1 = call(handler, "vault_write_memory", relpath="preferences.md", body="initial")
    assert res1["ok"] is True
    assert res1["overwritten"] is False
    p = vault / res1["path"]
    assert p.exists()

    res2 = call(handler, "vault_write_memory", relpath="preferences.md", body="updated")
    assert res2["ok"] is True
    assert res2["overwritten"] is True
    assert "updated" in p.read_text(encoding="utf-8")


def test_write_memory_allows_subdirs(vault, handler):
    res = call(handler, "vault_write_memory", relpath="people/alice.md", body="hi alice")
    assert res["ok"] is True
    assert (vault / "agents" / "hermes" / "memory" / "people" / "alice.md").exists()


def test_write_memory_rejects_traversal(vault, handler):
    for bad in ["../escape.md", "/etc/passwd", "people/../../../etc/passwd", "people/UPPER.md"]:
        res = call(handler, "vault_write_memory", relpath=bad, body="x")
        assert res["ok"] is False, f"path {bad!r} should have been rejected"
        assert res["reason"] in ("path_unsafe",)


def test_write_memory_requires_md_extension(vault, handler):
    res = call(handler, "vault_write_memory", relpath="no-ext", body="x")
    assert res["ok"] is False
    assert res["reason"] == "path_unsafe"


def test_write_memory_sets_last_compiled(vault, handler):
    res = call(handler, "vault_write_memory", relpath="prefs.md", body="x")
    p = vault / res["path"]
    txt = p.read_text(encoding="utf-8")
    today = _dt.date.today().isoformat()
    assert f"last_compiled: {today}" in txt
    assert "last_compiled_by: hermes" in txt


def test_write_memory_refuses_symlinked_memory_dir(vault, handler):
    import shutil
    mdir = vault / "agents" / "hermes" / "memory"
    shutil.rmtree(mdir)
    (vault.parent / "outside_mem").mkdir()
    os.symlink(vault.parent / "outside_mem", mdir)
    res = call(handler, "vault_write_memory", relpath="a.md", body="x")
    assert res["ok"] is False
    assert res["reason"] in ("memory_dir_is_symlink", "path_unsafe")


def test_write_memory_defeats_pre_placed_tmp_symlink(vault, handler):
    # Pre-create memory/x.md.tmp as a symlink to a file outside the vault.
    # The handler must NOT follow it — write must succeed at the real x.md,
    # and the outside file must remain untouched.
    outside = vault.parent / "victim.md"
    outside.write_text("ORIGINAL", encoding="utf-8")
    mdir = vault / "agents" / "hermes" / "memory"
    sym = mdir / "x.md.tmp"
    os.symlink(outside, sym)
    res = call(handler, "vault_write_memory", relpath="x.md", body="NEW")
    assert res["ok"] is True
    # The outside file MUST still contain ORIGINAL, not NEW.
    assert outside.read_text(encoding="utf-8") == "ORIGINAL"


# ---------- vault_conflict_scan ----------

def test_conflict_scan_hermes_scope(vault, handler):
    (vault / "agents" / "hermes" / "observations" / "a.sync-conflict-20260101-000000-AAA.md").write_text(
        "x", encoding="utf-8"
    )
    (vault / "decisions").mkdir()
    (vault / "decisions" / "d.sync-conflict-20260101-000000-BBB.md").write_text("x", encoding="utf-8")
    res = call(handler, "vault_conflict_scan")
    assert res["count"] == 1
    assert res["blocking"] is True


def test_conflict_scan_vault_scope_finds_all(vault, handler):
    (vault / "agents" / "hermes" / "observations" / "a.sync-conflict-20260101-000000-AAA.md").write_text(
        "x", encoding="utf-8"
    )
    (vault / "decisions").mkdir()
    (vault / "decisions" / "d.sync-conflict-20260101-000000-BBB.md").write_text("x", encoding="utf-8")
    res = call(handler, "vault_conflict_scan", scope="vault")
    assert res["count"] == 2


def test_conflict_scan_rejects_bad_scope(vault, handler):
    res = call(handler, "vault_conflict_scan", scope="not-a-scope")
    assert res["ok"] is False
    assert res["reason"] == "scope_invalid"


def test_conflict_scan_does_not_follow_symlink_cycles(vault, handler):
    # Create a symlink cycle inside agents/hermes/. If we don't followlinks=False,
    # this would loop forever.
    obs = vault / "agents" / "hermes" / "observations"
    os.symlink(vault / "agents" / "hermes", obs / "loop")
    res = call(handler, "vault_conflict_scan")
    assert res["ok"] is True  # didn't loop


# ---------- frontmatter parser robustness ----------

def test_frontmatter_handles_crlf_fences(vault, handler):
    p = vault / "areas" / "crlf.md"
    p.write_bytes(b"---\r\nlast_compiled: 2020-01-01\r\n---\r\n# old\r\n")
    res = call(handler, "vault_read", path="areas/crlf.md")
    assert res["ok"] is True
    assert res["staleness"]["state"] == "stale"


def test_frontmatter_handles_utf8_bom(vault, handler):
    p = vault / "areas" / "bom.md"
    p.write_bytes("﻿---\nlast_compiled: 2020-01-01\n---\n# old\n".encode("utf-8"))
    res = call(handler, "vault_read", path="areas/bom.md")
    assert res["ok"] is True
    assert res["staleness"]["state"] == "stale"


def test_frontmatter_strips_inline_comment(vault, handler):
    p = vault / "areas" / "comments.md"
    p.write_text(
        "---\nlast_compiled: 2020-01-01 # reviewed last year\n---\n# old\n",
        encoding="utf-8",
    )
    res = call(handler, "vault_read", path="areas/comments.md")
    assert res["ok"] is True
    assert res["staleness"]["state"] == "stale"


def test_frontmatter_unparseable_date_is_unknown_not_fresh(vault, handler):
    p = vault / "areas" / "bad.md"
    p.write_text("---\nlast_compiled: NEVER\n---\n# x\n", encoding="utf-8")
    res = call(handler, "vault_read", path="areas/bad.md")
    assert res["ok"] is True
    assert res["staleness"]["state"] == "unknown"
    assert "unparseable" in (res["staleness"].get("note") or "")


def test_frontmatter_future_date_is_unknown(vault, handler):
    future = _dt.date.today() + _dt.timedelta(days=30)
    p = vault / "areas" / "future.md"
    p.write_text(f"---\nlast_compiled: {future.isoformat()}\n---\n# x\n", encoding="utf-8")
    res = call(handler, "vault_read", path="areas/future.md")
    assert res["ok"] is True
    assert res["staleness"]["state"] == "unknown"
    assert "future" in (res["staleness"].get("note") or "")


def test_frontmatter_iso_datetime_is_parsed(vault, handler):
    p = vault / "areas" / "iso.md"
    p.write_text("---\nlast_compiled: 2020-01-01T12:30:00\n---\n# x\n", encoding="utf-8")
    res = call(handler, "vault_read", path="areas/iso.md")
    assert res["ok"] is True
    assert res["staleness"]["state"] == "stale"
