"""Tiny dotenv parser. No external dep.

Reads KEY=VALUE lines. Supports `export KEY=VALUE`, `#` comments, and
double-quoted values with `\\n` / `\\"` escapes. Single-quoted values are
treated literally (no escapes), matching `bash` and `python-dotenv` behavior.
Does NOT do variable interpolation (e.g. $OTHER) — keeps the parser
predictable and removes a class of injection surprises.

If a key is already set in os.environ, we DO NOT overwrite it. This lets
tests inject env vars and have them take precedence over the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file and merge into os.environ (without overwriting).

    Returns the dict of keys actually applied.
    """
    if not path.exists():
        return {}
    applied: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            val = _strip_inline_comment(val)
            val = _unquote(val)
            if key not in os.environ:
                os.environ[key] = val
                applied[key] = val
    return applied


def _strip_inline_comment(val: str) -> str:
    if not val or val[0] in "\"'":
        return val
    hash_idx = val.find(" #")
    if hash_idx >= 0:
        return val[:hash_idx].rstrip()
    return val


def _unquote(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] == '"':
        inner = val[1:-1]
        return inner.encode("utf-8").decode("unicode_escape")
    if len(val) >= 2 and val[0] == val[-1] == "'":
        return val[1:-1]
    return val
