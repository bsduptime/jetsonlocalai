"""Allowlist loader.

Reads `allowlist.yaml` and produces a mapping `email_lower -> daily_limit`.

Important behaviors:
  - File missing / empty / parse-fails / dependency-missing → treated as
    "no contacts allowed" externally (callers get `not_in_allowlist`).
    Internal cause goes to the audit log.
  - In-memory last-known-good cache. On parse failure mid-runtime, we keep
    serving from the cache and log the failure. This prevents an editor
    in-flight (truncated write) from causing a deny-all storm.

We accept JSON as a fallback if pyyaml is missing — handy in tests and means
the plugin still works after install if the user hasn't installed pyyaml yet.
The README documents the YAML format as the recommended one.
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
    contacts: dict[str, dict]  # email_lower -> {"daily_limit": int, "note": str?}
    loaded_from_mtime: float
    last_error: str | None = None


_cache: _CachedAllowlist | None = None


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
    # JSON fallback. Some YAML is valid JSON; both reasonable here.
    try:
        return json.loads(text)
    except Exception as e:
        raise ValueError(f"json parse failed (pyyaml not installed): {e}") from e


def _normalize_entries(obj: dict) -> dict[str, dict]:
    if not isinstance(obj, dict):
        raise ValueError("top-level must be a mapping with `contacts`")
    contacts = obj.get("contacts", []) or []
    if not isinstance(contacts, list):
        raise ValueError("`contacts` must be a list")
    out: dict[str, dict] = {}
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
        out[email_lc] = {
            "daily_limit": limit_raw,
            "note": note if isinstance(note, str) else None,
        }
    return out


def load_allowlist(path: Path) -> tuple[dict[str, dict], str | None]:
    """Return (contacts, internal_error). On error, contacts is the LKG cache
    (possibly empty). internal_error is a short string for the audit log.

    Reads the file with O_NOFOLLOW so a symlink replacing the allowlist
    is treated as a load failure rather than silently followed to an
    arbitrary file. (Codex F3.)
    """
    global _cache
    with _lock:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags)
        except FileNotFoundError:
            return ({}, "allowlist_file_missing")
        except OSError as e:
            err = f"open_failed_or_symlink: {e}"
            if _cache is not None:
                return (_cache.contacts, err)
            return ({}, err)
        try:
            try:
                st = os.fstat(fd)
            except OSError as e:
                if _cache is not None:
                    return (_cache.contacts, f"stat_failed: {e}")
                return ({}, f"stat_failed: {e}")
            try:
                chunks = []
                total = 0
                while True:
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > (1 << 20):  # 1 MiB sanity cap
                        err = "allowlist_too_large"
                        if _cache is not None:
                            return (_cache.contacts, err)
                        return ({}, err)
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8")
            except OSError as e:
                if _cache is not None:
                    return (_cache.contacts, f"read_failed: {e}")
                return ({}, f"read_failed: {e}")
            except UnicodeDecodeError as e:
                if _cache is not None:
                    return (_cache.contacts, f"utf8_decode_failed: {e}")
                return ({}, f"utf8_decode_failed: {e}")
        finally:
            os.close(fd)
        try:
            obj = _parse_yaml_or_json(text)
            contacts = _normalize_entries(obj)
        except ValueError as e:
            if _cache is not None:
                _cache.last_error = str(e)
                return (_cache.contacts, f"parse_failed_using_cache: {e}")
            return ({}, f"parse_failed: {e}")
        _cache = _CachedAllowlist(
            contacts=contacts,
            loaded_from_mtime=st.st_mtime,
            last_error=None,
        )
        return (contacts, None)


def lookup(email: str, contacts: dict[str, dict]) -> dict | None:
    """Case-insensitive whole-address match. Returns the entry dict or None."""
    return contacts.get(email.strip().lower())


# Test hook
def _reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
