#!/usr/bin/env python3
"""Append a `vault-health` block to today's daily/YYYY-MM-DD.md note.

Designed to run AFTER vault-conflict-scan.py so any stray conflict files
have already been moved out of the live tree. Produces a self-contained
block that David sees in his Daily Notes view.

Health signals:
- count of `*.sync-conflict-*` still in the live tree (should be 0)
- count of inbox/conflicts/<today>/ entries (today's freshly-moved batch)
- count of inbox/conflicts/ folders older than 14 days (review debt)
- count of inbox/ items > 14 days old (capture debt)
- oldest `last_compiled:` across compiled-state files in projects/, areas/, resources/, decisions/
- .stversions/ disk usage and filesystem free-% on the vault filesystem

Exit codes:
  0 — block written
  1 — unrecoverable error
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_VAULT = Path("/home/dbexpertai/obsidian-vault")
COMPILED_DIRS = ("projects", "areas", "resources", "decisions")
INBOX_STALENESS_DAYS = 14

_FRONTMATTER_RE = re.compile(
    r"^(?:﻿)?---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL
)
_LAST_COMPILED_RE = re.compile(
    r"^last_compiled:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.MULTILINE
)


def _walk_no_versions(root: Path):
    EXCLUDED = {".stversions", ".stfolder", ".obsidian", ".git"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED]
        yield Path(dirpath), dirnames, filenames


def count_live_conflicts(vault: Path) -> int:
    n = 0
    for dirpath, dirnames, filenames in _walk_no_versions(vault):
        if dirpath.is_relative_to(vault / "inbox" / "conflicts"):
            continue
        for name in filenames + dirnames:
            if ".sync-conflict-" in name:
                n += 1
    return n


def count_inbox_old(vault: Path, days: int) -> int:
    inbox = vault / "inbox"
    if not inbox.is_dir():
        return 0
    cutoff = _dt.datetime.now() - _dt.timedelta(days=days)
    n = 0
    for entry in inbox.iterdir():
        if entry.name == "conflicts":
            continue
        try:
            mtime = _dt.datetime.fromtimestamp(entry.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            n += 1
    return n


def count_conflict_folders_old(vault: Path, days: int) -> int:
    cdir = vault / "inbox" / "conflicts"
    if not cdir.is_dir():
        return 0
    cutoff = _dt.date.today() - _dt.timedelta(days=days)
    n = 0
    for entry in cdir.iterdir():
        if not entry.is_dir():
            continue
        try:
            folder_date = _dt.date.fromisoformat(entry.name)
        except ValueError:
            continue
        if folder_date < cutoff:
            n += 1
    return n


def count_today_conflicts(vault: Path) -> int:
    today_dir = vault / "inbox" / "conflicts" / _dt.date.today().isoformat()
    if not today_dir.is_dir():
        return 0
    return sum(1 for e in today_dir.iterdir() if e.name != "_INDEX.md")


def oldest_last_compiled(vault: Path) -> tuple[str | None, _dt.date | None]:
    """Walk compiled-state dirs, parse `last_compiled` from frontmatter, return oldest."""
    oldest_date: _dt.date | None = None
    oldest_path: str | None = None
    for sub in COMPILED_DIRS:
        root = vault / sub
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in _walk_no_versions(root):
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                p = Path(dirpath) / name
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        head = f.read(2048)
                except OSError:
                    continue
                m = _FRONTMATTER_RE.match(head)
                if not m:
                    continue
                lc = _LAST_COMPILED_RE.search(m.group(1))
                if not lc:
                    continue
                try:
                    d = _dt.date.fromisoformat(lc.group(1))
                except ValueError:
                    continue
                if oldest_date is None or d < oldest_date:
                    oldest_date = d
                    oldest_path = str(p.relative_to(vault))
    return oldest_path, oldest_date


def stversions_size_bytes(vault: Path) -> int:
    sv = vault / ".stversions"
    if not sv.is_dir():
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(sv, followlinks=False):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def disk_free_pct(vault: Path) -> tuple[int, int, float]:
    """Return (total_bytes, free_bytes, free_pct)."""
    usage = shutil.disk_usage(vault)
    pct = (usage.free / usage.total) * 100.0 if usage.total else 0.0
    return usage.total, usage.free, pct


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def render_block(vault: Path) -> str:
    today = _dt.date.today()
    live_conflicts = count_live_conflicts(vault)
    today_conflicts = count_today_conflicts(vault)
    old_conflict_folders = count_conflict_folders_old(vault, INBOX_STALENESS_DAYS)
    old_inbox = count_inbox_old(vault, INBOX_STALENESS_DAYS)
    oldest_path, oldest_date = oldest_last_compiled(vault)
    sv_bytes = stversions_size_bytes(vault)
    total, free, free_pct = disk_free_pct(vault)
    oldest_str = f"`{oldest_path}` ({oldest_date.isoformat()})" if oldest_date else "(none)"

    lines = [
        "## vault-health " + today.isoformat(),
        "",
        f"- live sync-conflict files in vault: **{live_conflicts}** (should be 0; conflict-scan cron runs first)",
        f"- conflicts moved to inbox today: **{today_conflicts}**",
        f"- conflict folders >{INBOX_STALENESS_DAYS} days old (review debt): **{old_conflict_folders}**",
        f"- inbox/ items >{INBOX_STALENESS_DAYS} days old (capture debt): **{old_inbox}**",
        f"- oldest `last_compiled` in compiled state: {oldest_str}",
        f"- `.stversions/` size: {human(sv_bytes)}",
        f"- vault filesystem free: {human(free)} of {human(total)} ({free_pct:.1f}%)",
        "",
        "_Generated by vault-crons/vault-health.py — run daily as dbexpertai._",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--print-only", action="store_true", help="Print block to stdout, don't write")
    args = parser.parse_args(argv)

    vault: Path = args.vault.resolve()
    if not vault.is_dir():
        print(f"ERROR: vault not found: {vault}", file=sys.stderr)
        return 1

    block = render_block(vault)

    if args.print_only:
        print(block)
        return 0

    try:
        daily_dir = vault / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        today = _dt.date.today()
        daily_file = daily_dir / f"{today.isoformat()}.md"

        # If the file already has a vault-health block from today, replace it
        # rather than accumulating duplicates from re-runs.
        if daily_file.exists():
            existing = daily_file.read_text(encoding="utf-8", errors="replace")
            pattern = re.compile(
                r"\n?## vault-health " + re.escape(today.isoformat()) + r"\n.*?(?=\n## |\Z)",
                re.DOTALL,
            )
            if pattern.search(existing):
                updated = pattern.sub("\n" + block, existing, count=1)
                daily_file.write_text(updated, encoding="utf-8")
            else:
                with open(daily_file, "a", encoding="utf-8") as f:
                    f.write("\n" + block)
        else:
            daily_file.write_text(
                f"---\ncreated: {today.isoformat()}\n---\n\n# {today.isoformat()}\n\n{block}",
                encoding="utf-8",
            )
    except OSError as e:
        print(f"ERROR: failed to write daily note: {e}", file=sys.stderr)
        return 1

    print(f"wrote vault-health block to {daily_file.relative_to(vault)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
