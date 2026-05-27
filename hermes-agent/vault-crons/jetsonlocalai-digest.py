#!/usr/bin/env python3
"""Daily digest of jetsonlocalai git activity into the shared vault.

Runs as `dbexpertai` from a daily cron. Output:
  /home/dbexpertai/obsidian-vault/projects/jetsonlocalai/digests/YYYY-MM-DD.md

Captures: commits authored since previous run (or since cutoff), the
HEAD/branch state, file paths touched, and a short stat block.
Designed so the librarian (future) can read these as compiled inputs.

Re-running the same day overwrites the day's digest. Days with no
activity write an empty-but-valid digest noting "no activity".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = Path("/home/dbexpertai/code/jetsonlocalai")
DEFAULT_VAULT = Path("/home/dbexpertai/obsidian-vault")
DIGESTS_SUBPATH = Path("projects/jetsonlocalai/digests")


class GitError(RuntimeError):
    pass


def _git_run(repo: Path, *args: str, with_safe: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if with_safe:
        cmd += ["-c", f"safe.directory={repo}"]
    cmd += list(args)
    return subprocess.run(cmd, cwd=repo, check=False, capture_output=True, text=True)


def _git_safe(repo: Path, *args: str) -> str:
    """Run git, retry with -c safe.directory on failure. Raise GitError if both fail."""
    res = _git_run(repo, *args)
    if res.returncode == 0 and res.stdout != "":
        return res.stdout
    # Retry once with safe.directory in case ownership is "dubious"
    res2 = _git_run(repo, *args, with_safe=True)
    if res2.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed: rc={res2.returncode} stderr={res2.stderr.strip()!r}"
        )
    return res2.stdout


def _sum_shortstat(text: str) -> tuple[int, int, int]:
    """Sum 'N files changed, X insertions(+), Y deletions(-)' lines across commits."""
    import re as _re
    files = ins = dels = 0
    for line in text.splitlines():
        m = _re.match(r"\s*(\d+)\s+files?\s+changed", line)
        if m:
            files += int(m.group(1))
        m = _re.search(r"(\d+)\s+insertions?\(\+\)", line)
        if m:
            ins += int(m.group(1))
        m = _re.search(r"(\d+)\s+deletions?\(-\)", line)
        if m:
            dels += int(m.group(1))
    return files, ins, dels


def render_digest(repo: Path, day: _dt.date) -> str:
    since = day.isoformat() + " 00:00:00"
    until = (day + _dt.timedelta(days=1)).isoformat() + " 00:00:00"

    # Capture errors per-call so a single git failure doesn't crash the digest;
    # surface as a clear sentinel in the output.
    git_errors: list[str] = []

    def safe(*args: str) -> str:
        try:
            return _git_safe(repo, *args)
        except GitError as e:
            git_errors.append(str(e))
            return ""

    # %H = full sha, %h = short, %an = author, %s = subject, %ci = committer ISO date
    log_format = "%H%x09%h%x09%an%x09%ci%x09%s"
    commits = safe(
        "log",
        f"--since={since}",
        f"--until={until}",
        "--all",
        f"--pretty=format:{log_format}",
        "--no-merges",
    ).strip()

    branch = safe("rev-parse", "--abbrev-ref", "HEAD").strip() or "(detached?)"
    head_sha = safe("rev-parse", "HEAD").strip()[:12]
    status_short = safe("status", "--short").strip()

    # Files touched in the window
    files_raw = safe(
        "log",
        f"--since={since}",
        f"--until={until}",
        "--all",
        "--no-merges",
        "--name-only",
        "--pretty=format:",
    )
    files = sorted({line for line in files_raw.splitlines() if line.strip()})

    # Shortstat aggregated as repo-wide totals (NOT per-commit).
    shortstat = safe(
        "log",
        f"--since={since}",
        f"--until={until}",
        "--all",
        "--no-merges",
        "--shortstat",
        "--pretty=format:",
    )
    n_files, n_ins, n_dels = _sum_shortstat(shortstat)
    shortstat_summary = (
        f"{n_files} file(s) changed, {n_ins} insertion(s)(+), {n_dels} deletion(s)(-)"
        if n_files or n_ins or n_dels
        else ""
    )

    body_lines = [
        f"---",
        f"date: {day.isoformat()}",
        f"repo: jetsonlocalai",
        f"head_branch: {branch}",
        f"head_sha: {head_sha}",
        f"last_compiled: {_dt.date.today().isoformat()}",
        f"last_compiled_by: jetsonlocalai-digest.py",
    ]
    if git_errors:
        body_lines.append(f"digest_errors: {len(git_errors)}")
    body_lines += [
        f"---",
        "",
        f"# jetsonlocalai digest — {day.isoformat()}",
        "",
    ]
    if git_errors:
        body_lines.append("> **WARNING**: git subprocess failures occurred. Digest may be incomplete:")
        body_lines.append("")
        for err in git_errors:
            body_lines.append(f"> - `{err}`")
        body_lines.append("")

    if not commits:
        body_lines.append("_No commit activity in this window._")
        body_lines.append("")
    else:
        commit_lines = commits.split("\n")
        body_lines.append(f"## Commits ({len(commit_lines)})")
        body_lines.append("")
        for line in commit_lines:
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            full_sha, short, author, ci_date, subject = parts
            body_lines.append(f"- `{short}` {author} — {subject}")
        body_lines.append("")

        if shortstat_summary:
            body_lines.append("## Shortstat")
            body_lines.append("")
            body_lines.append(f"```\n{shortstat_summary}\n```")
            body_lines.append("")

        if files:
            body_lines.append(f"## Files touched ({len(files)})")
            body_lines.append("")
            for f in files:
                body_lines.append(f"- `{f}`")
            body_lines.append("")

    body_lines.append(f"## HEAD state at digest time")
    body_lines.append("")
    body_lines.append(f"- branch: `{branch}`")
    body_lines.append(f"- sha: `{head_sha}`")
    if status_short:
        body_lines.append(f"- working tree:")
        body_lines.append(f"```\n{status_short}\n```")
    else:
        body_lines.append("- working tree: clean")
    body_lines.append("")
    return "\n".join(body_lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--print-only", action="store_true")
    args = parser.parse_args(argv)

    repo: Path = args.repo.resolve()
    vault: Path = args.vault.resolve()
    if not (repo / ".git").is_dir():
        print(f"ERROR: not a git repo: {repo}", file=sys.stderr)
        return 1
    if not vault.is_dir():
        print(f"ERROR: vault not found: {vault}", file=sys.stderr)
        return 1

    if args.date:
        try:
            day = _dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            return 2
    else:
        # The cron runs at 04:15; "today" so far has minimal activity. Default
        # to YESTERDAY so the digest summarizes a full calendar day.
        day = _dt.date.today() - _dt.timedelta(days=1)

    digest = render_digest(repo, day)

    if args.print_only:
        print(digest)
        return 0

    try:
        digest_dir = vault / DIGESTS_SUBPATH
        digest_dir.mkdir(parents=True, exist_ok=True)
        out = digest_dir / f"{day.isoformat()}.md"
        out.write_text(digest, encoding="utf-8")
    except OSError as e:
        print(f"ERROR: failed to write digest: {e}", file=sys.stderr)
        return 1

    print(f"wrote {out.relative_to(vault)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
