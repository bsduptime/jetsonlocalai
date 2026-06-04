"""Append-only JSONL audit log.

Every accepted/denied op is recorded with caller, op, outcome, and a
small set of non-secret fields. Never logs the API key, the JWT, or full
client PII beyond what's needed to trace an action (we log client ids and
truncated names, not full address blocks).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def append(path: Path, record: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Open with O_APPEND so concurrent worker threads don't interleave.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        # Auditing must never break the request path. A failed write is
        # swallowed; the daemon's stderr log still has the event.
        pass


def trunc(value, n: int = 80) -> str:
    if value is None:
        return ""
    s = str(value)
    return s if len(s) <= n else s[:n] + "…"
