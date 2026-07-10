"""Deterministic list state — one JSON file per named list, atomic writes.

Everything fuzzy stays in the model; nothing fuzzy is allowed in here.
Items are deduped on a normalized key (casefold + whitespace collapse) so
"Dog Food" and "dog  food" are one entry, but the display text is stored
as first given. Writes go tmp-file → os.replace, so a crash mid-write can
never leave a half-written (unparseable) list; a module lock serializes
concurrent tool calls within the single Hermes process.

Portable by construction: pathlib + os.replace only, no POSIX-isms — the
same file works on the Jetson and on a Windows mini PC.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path

_LIST_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_LOCK = threading.Lock()


@contextlib.contextmanager
def _os_lock(name: str):
    """Cross-PROCESS exclusive lock per list. The threading lock only covers
    this process; the board service (household-board/) shares these files, so
    read-modify-write cycles must also lock at the OS level or a plugin write
    and a board tap could silently drop each other's items. flock on POSIX,
    msvcrt.locking on Windows."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / f"{name}.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
MAX_ITEMS = 200
MAX_ITEM_CHARS = 120
MAX_QTY_CHARS = 40


class StoreError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def state_dir() -> Path:
    explicit = os.environ.get("HOUSEHOLD_STATE_DIR")
    if explicit:
        return Path(explicit)
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home) / "household"
    return Path.home() / ".hermes" / "household"


def normalize_key(item: str) -> str:
    return re.sub(r"\s+", " ", item).strip().casefold()


def _validate_list_name(name: str) -> str:
    # No defaulting here — the handler supplies "shopping"; the store is strict.
    name = (name if isinstance(name, str) else "").strip().casefold()
    # The name becomes a filename — the regex forbids separators and dots,
    # so a hostile "../x" can never leave the state dir.
    if not _LIST_NAME_RE.match(name):
        raise StoreError("invalid_list_name", name[:40])
    return name


def _path_for(name: str) -> Path:
    return state_dir() / f"{name}.json"


def _load(name: str) -> list[dict]:
    path = _path_for(name)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise StoreError("state_unreadable", type(e).__name__)
    try:
        data = json.loads(raw)
        items = data["items"]
        assert isinstance(items, list)
    except Exception:
        raise StoreError("state_corrupt", str(path))
    return items


def _save(name: str, items: list[dict]) -> None:
    path = _path_for(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"v": 1, "items": items},
                             ensure_ascii=False, indent=1)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise StoreError("state_unwritable", type(e).__name__)


def _clean_text(value: object, max_chars: int, field: str) -> str:
    if not isinstance(value, str):
        raise StoreError("invalid_field_type", field)
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        raise StoreError("empty_value", field)
    if len(text) > max_chars:
        raise StoreError("value_too_long", f"{field}>{max_chars}")
    return text


def add(list_name: str, entries: list[dict]) -> dict:
    """entries: [{"item": str, "qty": str?, "added_by": str?}, ...].
    Returns {"added": [...], "already_present": [...], "items": [...]}."""
    name = _validate_list_name(list_name)
    with _LOCK, _os_lock(name):
        items = _load(name)
        by_key = {it["key"]: it for it in items}
        added, already = [], []
        changed = False
        for e in entries:
            if not isinstance(e, dict):
                raise StoreError("entry_not_object", "")
            text = _clean_text(e.get("item"), MAX_ITEM_CHARS, "item")
            key = normalize_key(text)
            qty = e.get("qty")
            qty = _clean_text(qty, MAX_QTY_CHARS, "qty") if qty is not None else None
            added_by = e.get("added_by")
            added_by = (_clean_text(added_by, MAX_ITEM_CHARS, "added_by")
                        if added_by is not None else None)
            existing = by_key.get(key)
            if existing is not None:
                if existing.get("done"):
                    # It was bought and is being asked for again — that's a
                    # fresh need, not a duplicate: re-open it.
                    existing.pop("done", None)
                    if qty:
                        existing["qty"] = qty
                    changed = True
                    added.append(existing["item"])
                    continue
                # Same open item again: not an error, and a fresher qty wins
                # ("milk" ... later "2 milk") — everything else is a no-op.
                if qty and existing.get("qty") != qty:
                    existing["qty"] = qty
                    changed = True
                already.append(existing["item"])
                continue
            entry = {"key": key, "item": text,
                     "added_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            if qty:
                entry["qty"] = qty
            if added_by:
                entry["added_by"] = added_by
            if len(items) >= MAX_ITEMS:
                raise StoreError("list_full", str(MAX_ITEMS))
            items.append(entry)
            by_key[key] = entry
            added.append(text)
        if added or changed:
            _save(name, items)
        return {"list": name, "added": added, "already_present": already,
                "items": _public(items)}


def remove(list_name: str, item_names: list[str]) -> dict:
    name = _validate_list_name(list_name)
    with _LOCK, _os_lock(name):
        items = _load(name)
        removed, not_found = [], []
        for raw in item_names:
            text = _clean_text(raw, MAX_ITEM_CHARS, "item")
            key = normalize_key(text)
            match = next((it for it in items if it["key"] == key), None)
            if match is None:
                not_found.append(text)
                continue
            items.remove(match)
            removed.append(match["item"])
        if removed:
            _save(name, items)
        return {"list": name, "removed": removed, "not_found": not_found,
                "items": _public(items)}


def check(list_name: str, item_names: list[str], done: bool = True) -> dict:
    """Mark items bought (done=True) or re-open them (done=False). The item
    stays on the list either way — check-off is state, not deletion, so a
    mis-tap is reversible and the trip's progress is visible."""
    name = _validate_list_name(list_name)
    with _LOCK, _os_lock(name):
        items = _load(name)
        updated, not_found = [], []
        for raw in item_names:
            text = _clean_text(raw, MAX_ITEM_CHARS, "item")
            key = normalize_key(text)
            match = next((it for it in items if it["key"] == key), None)
            if match is None:
                not_found.append(text)
                continue
            if done:
                match["done"] = True
            else:
                match.pop("done", None)
            updated.append(match["item"])
        if updated:
            _save(name, items)
        field = "checked" if done else "unchecked"
        return {"list": name, field: updated, "not_found": not_found,
                "items": _public(items)}


def read(list_name: str) -> dict:
    name = _validate_list_name(list_name)
    with _LOCK, _os_lock(name):
        items = _public(_load(name))
        return {"list": name, "items": items,
                "open_count": sum(1 for it in items if not it.get("done")),
                "checked_count": sum(1 for it in items if it.get("done"))}


def clear(list_name: str, scope: str = "all") -> dict:
    if scope not in ("all", "checked"):
        raise StoreError("invalid_scope", scope)
    name = _validate_list_name(list_name)
    with _LOCK, _os_lock(name):
        items = _load(name)
        if scope == "checked":
            kept = [it for it in items if not it.get("done")]
        else:
            kept = []
        removed = len(items) - len(kept)
        if removed:
            _save(name, kept)
        return {"list": name, "cleared": removed, "scope": scope,
                "items": _public(kept)}


def _public(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in it.items() if k != "key"} for it in items]
