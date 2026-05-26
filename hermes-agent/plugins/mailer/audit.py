"""JSONL audit log.

Single file appended to under `state/sent.log`. Mode 600 in a 700 directory
so only the hermes user and root can read.

Fields are deliberately minimal: subject truncated to 80 chars, attachment
basenames only (no full paths), NEVER the body, NEVER the API key.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(log_path: Path, event: dict[str, Any]) -> None:
    payload = {"ts": _now_iso(), **event}
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")
    with _lock:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Open in append mode; create with 600 if new.
        fd = os.open(
            str(log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
            0o600,
        )
        try:
            # Retry until all bytes are written. os.write can theoretically
            # return short under disk pressure / signal interruption.
            view = memoryview(data)
            written = 0
            while written < len(data):
                try:
                    n = os.write(fd, view[written:])
                except InterruptedError:
                    continue
                if n <= 0:
                    # Out-of-space or fd unwriteable; give up rather than spin.
                    break
                written += n
        finally:
            os.close(fd)


def trunc_subject(s: str | None, n: int = 80) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
