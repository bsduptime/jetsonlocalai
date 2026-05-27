"""Path-safety primitives for the vault plugin.

Single source of truth for vault root and per-tool write boundaries. All
write handlers route through `resolve_under()` so we get a real-path
resolution (defeats symlinks) before any open() / write() call.

Path roots are read from the `HERMES_VAULT_ROOT` env var on every call so
tests can redirect to a tmp dir without reloading the module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_DEFAULT_VAULT_ROOT = "/home/dbexpertai/obsidian-vault"


def vault_root() -> Path:
    return Path(os.environ.get("HERMES_VAULT_ROOT", _DEFAULT_VAULT_ROOT)).resolve()


def hermes_ns() -> Path:
    return vault_root() / "agents" / "hermes"


def observations_dir() -> Path:
    return hermes_ns() / "observations"


def memory_dir() -> Path:
    return hermes_ns() / "memory"


def drafts_dir() -> Path:
    return hermes_ns() / "drafts"


def index_file() -> Path:
    return vault_root() / "INDEX.md"


def schedule_file() -> Path:
    return vault_root() / "areas" / "schedule.md"


def daily_dir() -> Path:
    return vault_root() / "daily"


# Slug for observation filenames: lowercase, kebab, 1-50 chars, no leading/trailing dash.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")

# Memory filename: relative path under MEMORY_DIR. Each component must be
# a-z0-9 with dashes/underscores, file must end in .md. Reject any component
# that is "." or "..".
_MEMORY_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


class PathError(ValueError):
    """Raised when a path violates containment rules."""


def resolve_under(root: Path, relative: str) -> Path:
    """Resolve `relative` against `root` and assert the result stays under root.

    Defeats symlinks via Path.resolve(). Refuses absolute paths in `relative`
    so the caller can't accidentally escape via a leading slash.
    """
    if not relative:
        raise PathError("empty path")
    rel = Path(relative)
    if rel.is_absolute():
        raise PathError(f"absolute path not allowed: {relative!r}")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise PathError(f"path escapes root: {relative!r}")
    return candidate


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str):
        raise PathError("slug must be a string")
    if not _SLUG_RE.match(slug):
        raise PathError(
            f"invalid slug {slug!r}: must be 1-50 chars, lowercase alnum + dashes, "
            "no leading/trailing dash"
        )
    return slug


def validate_memory_relpath(relpath: str) -> str:
    """Validate a relative path under agents/hermes/memory/.

    Allows subdirs (e.g. "people/alice.md"). Each path component must match
    _MEMORY_COMPONENT_RE. Final component must end in .md.
    """
    if not isinstance(relpath, str) or not relpath:
        raise PathError("memory path must be a non-empty string")
    if relpath.startswith("/") or ".." in relpath.split("/"):
        raise PathError(f"unsafe memory path {relpath!r}")
    parts = relpath.split("/")
    if not parts[-1].endswith(".md"):
        raise PathError(f"memory file must end in .md: {relpath!r}")
    for i, part in enumerate(parts):
        check = part[:-3] if i == len(parts) - 1 else part
        if not _MEMORY_COMPONENT_RE.match(check):
            raise PathError(
                f"invalid memory path component {part!r} (lowercase alnum + dash/underscore only)"
            )
    return relpath
