"""Vault plugin handlers.

Each handler:
  - validates inputs against the contract
  - resolves paths through `_paths.resolve_under()` (defeats traversal)
  - guards all opens with `O_NOFOLLOW` and checks for symlinked parent dirs
    (defends against TOCTOU + Mac-sync-introduced symlinks)
  - caps file reads at `MAX_READ_BYTES` to bound memory/latency
  - returns a JSON string with {"ok": true|false, ...}; never raises uncaught
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from . import _frontmatter as _fm
from ._paths import (
    PathError,
    daily_dir,
    hermes_ns,
    index_file,
    memory_dir,
    observations_dir,
    resolve_under,
    schedule_file,
    validate_memory_relpath,
    validate_slug,
    vault_root,
)

DEFAULT_STALENESS_DAYS = 14
MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MiB cap per file read
MAX_SCAN_ENTRIES = 200_000  # cap rglob/walk traversal


def _err(error: str, reason: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": error, "reason": reason, **extra})


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, default=str)


def _is_conflict_path(p: Path) -> bool:
    return any(".sync-conflict-" in part for part in p.parts)


def _verify_no_symlink_ancestor(target: Path, root: Path) -> None:
    """Lstat each ancestor between root (exclusive) and target (inclusive).
    Raise PathError if any is a symlink. Narrows the TOCTOU window between
    resolve_under() and open(); for full openat() defense, see README."""
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        rel_parts = target_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise PathError(f"target {target} not under root {root}")
    cur = root_resolved
    for part in rel_parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise PathError(f"symlink in path: {cur}")


def _open_read_nofollow(path: Path) -> bytes:
    """Open a regular file with O_NOFOLLOW, read up to MAX_READ_BYTES + 1.

    Raises OSError on symlink target. Raises PathError on oversize file.
    """
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        data = os.read(fd, MAX_READ_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_READ_BYTES:
        raise PathError(f"file exceeds {MAX_READ_BYTES} bytes: {path}")
    return data


def _read_text_safe(path: Path) -> str:
    return _open_read_nofollow(path).decode("utf-8", errors="replace")


# -----------------------------------------------------------------------------
# vault_session_brief
# -----------------------------------------------------------------------------
def vault_session_brief(args: dict, **_kwargs) -> str:
    try:
        days = int(args.get("days", 7))
        if days < 1 or days > 31:
            return _err("invalid_input", "days_out_of_range", days=days)

        today = _dt.date.today()
        cutoff = today - _dt.timedelta(days=days - 1)

        index = _read_with_staleness(index_file()) if index_file().exists() else None
        schedule = _read_with_staleness(schedule_file()) if schedule_file().exists() else None

        daily: list[dict[str, Any]] = []
        ddir = daily_dir()
        if ddir.exists() and not ddir.is_symlink():
            for entry in sorted(ddir.glob("*.md")):
                if entry.is_symlink():
                    continue
                date = _parse_daily_filename(entry.stem)
                if date is None or date < cutoff or date > today:
                    continue
                try:
                    content = _read_text_safe(entry)
                except (OSError, PathError):
                    continue
                daily.append({
                    "date": date.isoformat(),
                    "path": str(entry.relative_to(vault_root())),
                    "content": content,
                })

        conflicts = _scan_conflicts(hermes_ns())
        warnings: list[str] = []
        for label, doc in (("INDEX.md", index), ("areas/schedule.md", schedule)):
            if doc and doc.get("staleness", {}).get("state") in ("stale", "unknown"):
                note = doc["staleness"].get("note") or f"`last_compiled` is older than {DEFAULT_STALENESS_DAYS} days"
                warnings.append(f"{label}: {note}")
        if conflicts:
            warnings.append(
                f"{len(conflicts)} sync-conflict files in agents/hermes/ — treat as ERROR and surface to David before proceeding."
            )

        return _ok(
            index=index,
            schedule=schedule,
            daily=daily,
            conflicts=conflicts,
            blocking=bool(conflicts),
            warnings=warnings,
            window_days=days,
        )
    except Exception as e:
        return _err("internal_error", "unhandled_exception", detail=type(e).__name__)


# -----------------------------------------------------------------------------
# vault_read
# -----------------------------------------------------------------------------
def vault_read(args: dict, **_kwargs) -> str:
    try:
        path_arg = args.get("path")
        if not isinstance(path_arg, str) or not path_arg:
            return _err("invalid_input", "path_required")
        try:
            target = resolve_under(vault_root(), path_arg)
        except PathError as e:
            return _err("invalid_input", "path_unsafe", detail=str(e))
        # is_symlink check on the UNRESOLVED path: resolve_under() canonicalizes
        # symlinks, so the resolved `target` is never a symlink itself. We
        # explicitly refuse the request if the user-supplied path or any of
        # its components is a symlink, even if it resolves to an in-vault file.
        unresolved = vault_root() / path_arg
        if unresolved.is_symlink():
            return _err("invalid_input", "is_symlink", path=path_arg)
        if not target.exists():
            return _err("not_found", "file_missing", path=path_arg)
        if not target.is_file():
            return _err("invalid_input", "not_a_file", path=path_arg)
        if _is_conflict_path(target):
            return _err(
                "blocked", "sync_conflict_file",
                path=str(target.relative_to(vault_root())),
                detail="Refusing to read a sync-conflict file. Surface to David for resolution.",
            )
        try:
            _verify_no_symlink_ancestor(target, vault_root())
        except PathError as e:
            return _err("invalid_input", "path_unsafe", detail=str(e))
        try:
            data = _read_with_staleness(target)
        except (OSError, PathError) as e:
            return _err("read_failed", type(e).__name__, detail=str(e))
        return _ok(**data)
    except Exception as e:
        return _err("internal_error", "unhandled_exception", detail=type(e).__name__)


# -----------------------------------------------------------------------------
# vault_write_observation
# -----------------------------------------------------------------------------
def vault_write_observation(args: dict, **_kwargs) -> str:
    try:
        slug = args.get("slug")
        body = args.get("body")
        sources = args.get("sources") or []
        if not isinstance(body, str) or not body.strip():
            return _err("invalid_input", "body_required")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            return _err("invalid_input", "sources_must_be_list_of_strings")
        try:
            slug = validate_slug(slug)
        except PathError as e:
            return _err("invalid_input", "slug_invalid", detail=str(e))

        now = _dt.datetime.now()
        ts = now.strftime("%Y-%m-%d-%H%M")
        filename = f"{ts}-{slug}.md"
        obs_dir = observations_dir()
        try:
            target = resolve_under(obs_dir, filename)
        except PathError as e:
            return _err("invalid_input", "path_unsafe", detail=str(e))

        # Ensure obs_dir exists and is not a symlink before opening files inside it.
        if obs_dir.is_symlink():
            return _err("invalid_input", "observations_dir_is_symlink")
        obs_dir.mkdir(parents=True, exist_ok=True)
        try:
            _verify_no_symlink_ancestor(target.parent, vault_root())
        except PathError as e:
            return _err("invalid_input", "path_unsafe", detail=str(e))

        fm: dict[str, Any] = {
            "created": now.replace(microsecond=0).isoformat(),
            "slug": slug,
        }
        if sources:
            fm["sources"] = sources
        text = _fm.render(fm) + body.rstrip() + "\n"
        payload = text.encode("utf-8")

        try:
            fd = os.open(
                str(target),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
            )
        except FileExistsError:
            return _err("conflict", "filename_exists", path=str(target.relative_to(vault_root())))
        except OSError as e:
            return _err("write_failed", "os_error", detail=f"{e.errno}:{e.strerror}")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
        except Exception as e:
            return _err("write_failed", "write_error", detail=type(e).__name__)
        return _ok(
            path=str(target.relative_to(vault_root())),
            filename=filename,
            bytes=len(payload),
        )
    except Exception as e:
        return _err("internal_error", "unhandled_exception", detail=type(e).__name__)


# -----------------------------------------------------------------------------
# vault_write_memory
# -----------------------------------------------------------------------------
def vault_write_memory(args: dict, **_kwargs) -> str:
    try:
        relpath = args.get("relpath")
        body = args.get("body")
        sources = args.get("sources") or []
        if not isinstance(body, str) or not body.strip():
            return _err("invalid_input", "body_required")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            return _err("invalid_input", "sources_must_be_list_of_strings")
        try:
            relpath = validate_memory_relpath(relpath)
            target = resolve_under(memory_dir(), relpath)
        except PathError as e:
            return _err("invalid_input", "path_unsafe", detail=str(e))

        mdir = memory_dir()
        if mdir.is_symlink():
            return _err("invalid_input", "memory_dir_is_symlink")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _verify_no_symlink_ancestor(target.parent, vault_root())
        except PathError as e:
            return _err("invalid_input", "path_unsafe", detail=str(e))

        # Preserve existing frontmatter keys (e.g. `created`) when overwriting.
        prior_fm: dict[str, Any] = {}
        if target.exists() and not target.is_symlink():
            try:
                prior_fm, _ = _fm.parse(_read_text_safe(target))
            except (OSError, PathError):
                prior_fm = {}

        fm: dict[str, Any] = dict(prior_fm)
        fm.setdefault("created", _dt.date.today().isoformat())
        fm["last_compiled"] = _dt.date.today().isoformat()
        fm["last_compiled_by"] = "hermes"
        if sources:
            fm["sources"] = sources

        text = _fm.render(fm) + body.rstrip() + "\n"
        payload = text.encode("utf-8")
        tmp = target.with_suffix(target.suffix + ".tmp")

        # Defeat a pre-placed symlink at the tmp path by refusing if any non-symlink
        # exists, AND requiring O_EXCL|O_NOFOLLOW on create.
        if tmp.exists() or tmp.is_symlink():
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return _err("write_failed", "tmp_exists", detail=str(tmp))

        try:
            fd = os.open(
                str(tmp),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
            )
        except OSError as e:
            return _err("write_failed", "os_error", detail=f"{e.errno}:{e.strerror}")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp, target)  # rename is symlink-safe at both ends
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return _err("write_failed", "os_error", detail=f"{e.errno}:{e.strerror}")
        return _ok(
            path=str(target.relative_to(vault_root())),
            bytes=len(payload),
            overwritten=bool(prior_fm),
        )
    except Exception as e:
        return _err("internal_error", "unhandled_exception", detail=type(e).__name__)


# -----------------------------------------------------------------------------
# vault_conflict_scan
# -----------------------------------------------------------------------------
def vault_conflict_scan(args: dict, **_kwargs) -> str:
    try:
        scope = (args.get("scope") or "hermes").lower()
        if scope == "hermes":
            root = hermes_ns()
        elif scope == "vault":
            root = vault_root()
        else:
            return _err("invalid_input", "scope_invalid", scope=scope)
        conflicts, truncated = _scan_conflicts_bounded(root)
        return _ok(
            conflicts=conflicts,
            scope=scope,
            count=len(conflicts),
            blocking=bool(conflicts),
            truncated=truncated,
        )
    except Exception as e:
        return _err("internal_error", "unhandled_exception", detail=type(e).__name__)


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _read_with_staleness(target: Path) -> dict[str, Any]:
    text = _read_text_safe(target)
    fm, body = _fm.parse(text)
    threshold = DEFAULT_STALENESS_DAYS
    raw_threshold = fm.get("staleness_warning_after_days")
    if isinstance(raw_threshold, int) and raw_threshold > 0:
        threshold = raw_threshold
    status = _fm.staleness_status(fm)
    # Re-bucket using the file's own threshold, if any.
    age = status["age_days"]
    if status["state"] == "fresh" and isinstance(age, int) and age > threshold:
        status = {"state": "stale", "age_days": age, "note": None}
    return {
        "path": str(target.relative_to(vault_root())),
        "frontmatter": fm,
        "body": body,
        "staleness": status,
        "staleness_threshold_days": threshold,
    }


def _scan_conflicts(root: Path) -> list[str]:
    conflicts, _ = _scan_conflicts_bounded(root)
    return conflicts


def _scan_conflicts_bounded(root: Path) -> tuple[list[str], bool]:
    """Walk `root` without following symlinks, return up to MAX_SCAN_ENTRIES matches.

    Returns (matches_sorted, truncated_bool).
    """
    if not root.exists():
        return [], False
    matches: list[str] = []
    visited = 0
    truncated = False
    vroot = vault_root()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            visited += 1
            if visited > MAX_SCAN_ENTRIES:
                truncated = True
                break
            if ".sync-conflict-" in name:
                full = Path(dirpath) / name
                try:
                    matches.append(str(full.relative_to(vroot)))
                except ValueError:
                    matches.append(str(full))
        if truncated:
            break
        # also flag conflict-named directories
        for name in dirnames:
            if ".sync-conflict-" in name:
                full = Path(dirpath) / name
                try:
                    matches.append(str(full.relative_to(vroot)))
                except ValueError:
                    matches.append(str(full))
    return sorted(matches), truncated


def _parse_daily_filename(stem: str) -> _dt.date | None:
    head = stem[:10]
    if len(head) != 10 or head[4] != "-" or head[7] != "-":
        return None
    try:
        return _dt.date.fromisoformat(head)
    except ValueError:
        return None
