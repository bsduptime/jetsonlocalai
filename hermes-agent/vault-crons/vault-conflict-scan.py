#!/usr/bin/env python3
"""Move Syncthing conflict files into inbox/conflicts/ for triage.

Designed to be run as `dbexpertai` from a daily cron. Scans the whole vault
(NOT just agents/hermes/) and moves any `*.sync-conflict-*` file or directory
into `inbox/conflicts/YYYY-MM-DD/` with the original path encoded into the
filename so collisions are impossible.

Idempotent: re-running on a vault with no conflicts is a no-op.

Exit codes:
  0 — scan succeeded (may have moved 0 or more files)
  1 — unrecoverable error (vault missing, inbox not writable, etc.)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

DEFAULT_VAULT = Path("/home/dbexpertai/obsidian-vault")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--dry-run", action="store_true")
    default_log = Path(os.environ.get("HOME", "/home/dbexpertai")) / ".local/share/vault-crons/conflict-scan.log"
    parser.add_argument("--log", type=Path, default=default_log)
    args = parser.parse_args(argv)

    vault: Path = args.vault.resolve()
    if not vault.is_dir():
        print(f"ERROR: vault not found: {vault}", file=sys.stderr)
        return 1

    today = _dt.date.today().isoformat()
    inbox = vault / "inbox" / "conflicts" / today
    if not args.dry_run:
        inbox.mkdir(parents=True, exist_ok=True)

    moved: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    # Don't descend into .stversions/ — Syncthing's internal version store.
    # The conflict-file pattern in there is expected (historical conflicts).
    EXCLUDED = {".stversions", ".stfolder"}

    for dirpath, dirnames, filenames in os.walk(vault, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED]
        # Skip the inbox/conflicts/ tree itself so we don't move files we
        # just moved.
        if Path(dirpath).is_relative_to(vault / "inbox" / "conflicts"):
            continue
        for name in filenames + dirnames:
            if ".sync-conflict-" not in name:
                continue
            src = Path(dirpath) / name
            rel = src.relative_to(vault)
            # Encode path-with-slashes into a flat filename so collisions
            # are impossible.
            dst_name = str(rel).replace("/", "__")
            dst = inbox / dst_name
            # Collision-safe: walk a counter suffix until we find a free name.
            counter = 1
            while dst.exists():
                dst = inbox / f"{dst_name}.dup{counter}"
                counter += 1
                if counter > 9999:
                    errors.append({"src": str(rel), "reason": "collision_exhausted"})
                    dst = None
                    break
            if dst is None:
                continue
            record = {"src": str(rel), "dst": str(dst.relative_to(vault))}
            if args.dry_run:
                skipped.append(record | {"reason": "dry-run"})
                continue
            try:
                shutil.move(str(src), str(dst))
                moved.append(record)
            except OSError as e:
                errors.append(record | {"reason": f"os_error:{e.errno}:{e.strerror}"})

    # Write the per-run log.
    if not args.dry_run:
        try:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                    "moved": moved,
                    "skipped": skipped,
                    "errors": errors,
                }) + "\n")
        except OSError as e:
            # Don't fail the scan if logging fails — print to stderr instead.
            print(f"WARN: could not write log: {e}", file=sys.stderr)

    # Also drop a per-day index file inside inbox/conflicts/<date>/ for
    # human review. Wrapped in try so a write failure here doesn't crash
    # after files have already moved.
    if moved and not args.dry_run:
        index = inbox / "_INDEX.md"
        try:
            with open(index, "w", encoding="utf-8") as f:
                f.write(f"# Sync conflicts moved {today}\n\n")
                f.write(f"Found {len(moved)} conflict file(s). Review each and either:\n")
                f.write("- Decide which side wins, edit, then delete the loser.\n")
                f.write("- Delete both if neither has useful content.\n\n")
                for m in moved:
                    f.write(f"- `{m['dst']}` ← was `{m['src']}`\n")
        except OSError as e:
            print(f"WARN: failed to write {index}: {e}", file=sys.stderr)
            errors.append({"index": str(index.relative_to(vault)), "reason": f"index_write_failed:{e.errno}"})

    print(f"scan complete: moved={len(moved)} skipped={len(skipped)} errors={len(errors)}")
    # Exit non-zero on partial failures so cron-level monitoring catches it.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
