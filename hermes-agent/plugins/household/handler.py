"""household plugin handlers — thin, validating shims over _store.

Same contract as the other plugins: validate field types up front so
simple LLM mistakes get a structured error instead of a traceback, then
call the deterministic store. ALWAYS returns a JSON string, never raises.
No relay/daemon here — there are no credentials and no irreversible side
effects behind these tools, just a JSON file, so a privilege boundary
would be pure overhead.
"""

from __future__ import annotations

import json

from . import _store


def _bad(reason: str, detail: str = "") -> str:
    return json.dumps({
        "ok": False, "error": "invalid_input",
        "reason": reason, "detail": detail,
    }, ensure_ascii=False)


def _run(fn, *fn_args) -> str:
    try:
        result = fn(*fn_args)
    except _store.StoreError as e:
        return json.dumps({"ok": False, "error": "state",
                           "reason": e.reason, "detail": e.detail},
                          ensure_ascii=False)
    except Exception as e:  # absolute backstop — a tool must never raise
        return json.dumps({"ok": False, "error": "internal",
                           "reason": type(e).__name__}, ensure_ascii=False)
    return json.dumps({"ok": True, **result}, ensure_ascii=False)


def shopping_add(args: dict, **_kwargs) -> str:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return _bad("invalid_field_type", "items")
    list_name = args.get("list") or "shopping"
    if not isinstance(list_name, str):
        return _bad("invalid_field_type", "list")
    return _run(_store.add, list_name, items)


def shopping_remove(args: dict, **_kwargs) -> str:
    items = args.get("items")
    if not isinstance(items, list) or not items \
            or not all(isinstance(i, str) for i in items):
        return _bad("invalid_field_type", "items")
    list_name = args.get("list") or "shopping"
    if not isinstance(list_name, str):
        return _bad("invalid_field_type", "list")
    return _run(_store.remove, list_name, items)


def shopping_list(args: dict, **_kwargs) -> str:
    list_name = (args or {}).get("list") or "shopping"
    if not isinstance(list_name, str):
        return _bad("invalid_field_type", "list")
    return _run(_store.read, list_name)


def shopping_clear(args: dict, **_kwargs) -> str:
    if args.get("confirm") is not True:
        return json.dumps({
            "ok": False, "error": "not_allowed",
            "reason": "confirmation_required",
            "detail": "ask the user, then call again with confirm=true",
        }, ensure_ascii=False)
    list_name = args.get("list") or "shopping"
    if not isinstance(list_name, str):
        return _bad("invalid_field_type", "list")
    return _run(_store.clear, list_name)
