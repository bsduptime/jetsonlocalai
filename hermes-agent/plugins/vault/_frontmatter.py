"""Minimal YAML frontmatter parser/serializer for the vault plugin.

We accept only flat scalar keys and flat YAML lists — that's all the schema
in shared-memory-architecture.md uses. Keeping the parser hand-rolled means
no runtime dependency on PyYAML inside the hermes venv.

Robustness choices (after Codex review):

- Strip a leading UTF-8 BOM before checking for an opening `---` fence.
- Strip CR from each line before comparing the closing fence (CRLF tolerant).
- Strip whole-line YAML comments (lines whose first non-space char is `#`).
- Strip an inline ` # comment` suffix (space-hash convention) from scalar
  values.
- `staleness_days()` distinguishes three states: `fresh`, `stale`, `unknown`.
  An `unknown` is treated as a warning rather than fail-open.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

_FENCE = "---"


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from `text`.

    Returns (frontmatter_dict, body). If the file has no frontmatter, returns
    ({}, text). Frontmatter must start at line 1 (after an optional BOM) with
    `---` and end with `---`. CRLF line endings are tolerated.
    """
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith(_FENCE):
        return {}, text
    lines = text.splitlines(keepends=True)
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _FENCE:
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_body = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    return _parse_flat_yaml(fm_body), body


def _strip_inline_comment(val: str) -> str:
    """Strip an inline ` # comment` suffix, preserving `#` inside quoted strings."""
    if not val:
        return val
    if val[0] in ("'", '"'):
        # quoted: find matching close, return value without quotes
        q = val[0]
        end = val.find(q, 1)
        if end >= 0:
            return val[1:end]
        return val[1:]
    # unquoted: split on " #" (space-hash) — the standard YAML inline-comment marker
    idx = val.find(" #")
    if idx >= 0:
        return val[:idx].rstrip()
    return val.rstrip()


def _parse_flat_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_list: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list is not None:
            current_list.append(_strip_inline_comment(stripped[2:].strip()))
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val == "":
            current_list = []
            result[key] = current_list
        else:
            current_list = None
            result[key] = _coerce_scalar(_strip_inline_comment(val))
    return result


def _coerce_scalar(val: str) -> Any:
    val = val.strip().strip("'\"")
    # Try date YYYY-MM-DD first
    if len(val) == 10 and val[4] == "-" and val[7] == "-":
        try:
            return _dt.date.fromisoformat(val)
        except ValueError:
            pass
    # Try full ISO datetime (with or without offset)
    try:
        # Python's fromisoformat handles "2026-05-27T23:30:00+03:00" since 3.11;
        # for 3.10 it handles plain "2026-05-27T23:30:00". Try both shapes.
        return _dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    try:
        return int(val)
    except ValueError:
        pass
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    return val


def render(fm: dict[str, Any]) -> str:
    """Render `fm` as a YAML frontmatter block including fences."""
    if not fm:
        return ""
    out = [_FENCE]
    for key, val in fm.items():
        if isinstance(val, list):
            out.append(f"{key}:")
            for item in val:
                out.append(f"  - {item}")
        elif isinstance(val, _dt.datetime):
            out.append(f"{key}: {val.isoformat()}")
        elif isinstance(val, _dt.date):
            out.append(f"{key}: {val.isoformat()}")
        elif isinstance(val, bool):
            out.append(f"{key}: {str(val).lower()}")
        else:
            out.append(f"{key}: {val}")
    out.append(_FENCE)
    out.append("")
    return "\n".join(out)


def staleness_status(fm: dict[str, Any], today: _dt.date | None = None) -> dict[str, Any]:
    """Three-state staleness check: fresh | stale | unknown.

    Returns {"state": str, "age_days": int|None, "note": str|None}.

    - `fresh`: last_compiled exists and is within window.
    - `stale`: last_compiled exists and is older than threshold.
    - `unknown`: last_compiled missing, unparseable, or in the future (clock skew).
      Callers should treat `unknown` the same as `stale` (warn the user).
    """
    today = today or _dt.date.today()
    raw = fm.get("last_compiled")
    if raw is None:
        return {"state": "unknown", "age_days": None, "note": "no last_compiled in frontmatter"}
    if isinstance(raw, _dt.datetime):
        last = raw.date()
    elif isinstance(raw, _dt.date):
        last = raw
    elif isinstance(raw, str):
        try:
            last = _dt.date.fromisoformat(raw)
        except ValueError:
            try:
                last = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            except (ValueError, TypeError):
                return {"state": "unknown", "age_days": None, "note": f"unparseable last_compiled: {raw!r}"}
    else:
        return {"state": "unknown", "age_days": None, "note": f"unexpected last_compiled type: {type(raw).__name__}"}

    age = (today - last).days
    if age < 0:
        return {
            "state": "unknown",
            "age_days": age,
            "note": f"last_compiled is in the future ({last.isoformat()}); clock skew?",
        }
    return {"state": "fresh" if age <= 14 else "stale", "age_days": age, "note": None}
