"""Per-caller allowlist loader.

Two file layouts supported:
  1. SINGLE-CALLER (default): /etc/hermes-mailer/allowlist.yaml
     Contents apply to caller="elena". This is the simple form for the
     current deployment.
  2. MULTI-CALLER (future-proof): /etc/hermes-mailer/allowlists/<caller>.yaml
     One file per caller. Used when the directory exists AND contains a
     file matching the calling identity.

Lookup precedence: multi-caller file wins if present; else fall back to
the single-caller file IF the caller is "elena" (the default identity for
backwards compatibility).

Last-known-good cache per caller — on parse/IO failure we keep serving
from the cache and log the failure (so an in-flight edit can't cause a
deny-all storm).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .headers import _EMAIL_RE

_lock = threading.Lock()


@dataclass
class _CachedAllowlist:
    contacts: dict[str, dict]
    last_error: str | None = None


_cache: dict[str, _CachedAllowlist] = {}  # caller -> cached entries


def _parse_yaml_or_json(text: str) -> dict:
    try:
        import yaml  # type: ignore
    except Exception:
        yaml = None
    if yaml is not None:
        try:
            obj = yaml.safe_load(text)
            if obj is None:
                obj = {}
            return obj
        except Exception as e:
            raise ValueError(f"yaml parse failed: {e}") from e
    try:
        return json.loads(text)
    except Exception as e:
        raise ValueError(f"json parse failed (pyyaml not installed): {e}") from e


def _normalize_aliases(raw, i: int) -> list[str]:
    """Validate + normalize an entry's optional `aliases` list.

    Each alias is a short human-friendly handle the AGENT uses to resolve a
    name to an address (e.g. "yoram", "me", "my email"). They are NOT a
    security boundary — the allowlist is still keyed by email and the daemon
    only ever sends to a literal allowlisted address. We normalize to
    lowercase + dedup within the entry and cap length so the directory the
    agent sees stays clean.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"contact[{i}].aliases must be a list")
    seen_local: set[str] = set()
    out: list[str] = []
    for j, a in enumerate(raw):
        if not isinstance(a, str):
            raise ValueError(f"contact[{i}].aliases[{j}] must be a string")
        a_lc = a.strip().lower()
        if not a_lc:
            raise ValueError(f"contact[{i}].aliases[{j}] is empty")
        if len(a_lc) > 64:
            raise ValueError(f"contact[{i}].aliases[{j}] too long (>64 chars)")
        if any(ord(c) < 0x20 for c in a_lc):
            raise ValueError(f"contact[{i}].aliases[{j}] has control chars")
        if a_lc in seen_local:
            raise ValueError(f"contact[{i}] duplicate alias within entry: {a_lc!r}")
        seen_local.add(a_lc)
        out.append(a_lc)
    return out


def _normalize_entries(obj: dict) -> dict[str, dict]:
    if not isinstance(obj, dict):
        raise ValueError("top-level must be a mapping with `contacts`")
    contacts = obj.get("contacts", []) or []
    if not isinstance(contacts, list):
        raise ValueError("`contacts` must be a list")
    out: dict[str, dict] = {}
    # Track aliases across ALL entries so the agent never sees an ambiguous
    # handle. An alias may not collide with another alias or with any email.
    alias_owner: dict[str, str] = {}
    for i, entry in enumerate(contacts):
        if not isinstance(entry, dict):
            raise ValueError(f"contact[{i}] must be a mapping")
        email = entry.get("email")
        if not isinstance(email, str):
            raise ValueError(f"contact[{i}].email must be a string")
        email_lc = email.strip().lower()
        if not _EMAIL_RE.fullmatch(email_lc):
            raise ValueError(f"contact[{i}].email not a valid address: {email!r}")
        limit_raw = entry.get("daily_limit")
        if not isinstance(limit_raw, int) or isinstance(limit_raw, bool):
            raise ValueError(f"contact[{i}].daily_limit must be int")
        if limit_raw < 1 or limit_raw > 100:
            raise ValueError(f"contact[{i}].daily_limit out of range (1..100)")
        if email_lc in out:
            raise ValueError(f"duplicate email: {email_lc}")
        note = entry.get("note")
        name = entry.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError(f"contact[{i}].name must be a string")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        aliases = _normalize_aliases(entry.get("aliases"), i)
        for a in aliases:
            if a in alias_owner and alias_owner[a] != email_lc:
                raise ValueError(
                    f"alias {a!r} on {email_lc} already used by {alias_owner[a]}")
            alias_owner[a] = a
        out[email_lc] = {
            "daily_limit": limit_raw,
            "note": note if isinstance(note, str) else None,
            "name": name,
            "aliases": aliases,
        }
    # An alias must never shadow a real email address either.
    for a in alias_owner:
        if a in out:
            raise ValueError(f"alias {a!r} collides with a contact email")
    return out


def _resolve_path(*, caller: str, single_path: Path,
                  allowlists_dir: Path) -> Path | None:
    """Resolve the allowlist FILE for this caller. Returns None if no file
    exists. Prefers the multi-caller form."""
    multi = allowlists_dir / f"{caller}.yaml"
    if multi.exists():
        return multi
    # Backwards-compat single-file form is ONLY for caller "elena".
    if caller == "elena" and single_path.exists():
        return single_path
    return None


def _read_file_with_nofollow(path: Path) -> tuple[bytes, str | None]:
    """Read using O_NOFOLLOW; returns (bytes, internal_error_or_None)."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return (b"", "allowlist_file_missing")
    except OSError as e:
        return (b"", f"open_failed_or_symlink: {e}")
    try:
        chunks = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError as e:
                return (b"", f"read_failed: {e}")
            if not chunk:
                break
            total += len(chunk)
            if total > (1 << 20):  # 1 MiB sanity cap
                return (b"", "allowlist_too_large")
            chunks.append(chunk)
        return (b"".join(chunks), None)
    finally:
        os.close(fd)


def load_for_caller(caller: str, *, single_path: Path,
                    allowlists_dir: Path) -> tuple[dict[str, dict], str | None]:
    """Return (contacts, internal_error_or_None) for the given caller.

    `contacts` is the LKG cache on error (possibly empty).
    `internal_error` is a short token for the audit log.
    """
    if not isinstance(caller, str) or not caller:
        return ({}, "invalid_caller")

    with _lock:
        path = _resolve_path(
            caller=caller, single_path=single_path,
            allowlists_dir=allowlists_dir,
        )
        if path is None:
            cached = _cache.get(caller)
            if cached is not None:
                return (cached.contacts, "allowlist_file_missing_using_cache")
            return ({}, "allowlist_file_missing")

        raw, err = _read_file_with_nofollow(path)
        if err is not None:
            cached = _cache.get(caller)
            if cached is not None:
                return (cached.contacts, f"{err}_using_cache")
            return ({}, err)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            cached = _cache.get(caller)
            if cached is not None:
                return (cached.contacts, f"utf8_decode_failed_using_cache: {e}")
            return ({}, f"utf8_decode_failed: {e}")
        try:
            obj = _parse_yaml_or_json(text)
            contacts = _normalize_entries(obj)
        except ValueError as e:
            cached = _cache.get(caller)
            if cached is not None:
                cached.last_error = str(e)
                return (cached.contacts, f"parse_failed_using_cache: {e}")
            return ({}, f"parse_failed: {e}")
        _cache[caller] = _CachedAllowlist(contacts=contacts, last_error=None)
        return (contacts, None)


def lookup(email: str, contacts: dict[str, dict]) -> dict | None:
    return contacts.get(email.strip().lower())


def _reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = {}
