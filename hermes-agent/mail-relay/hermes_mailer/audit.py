"""JSONL audit log — caller-aware.

Each line records: timestamp, request_id, caller, event, outcome,
recipient, truncated subject (80 chars), attachment basenames (no
full paths), byte count, message id, transport.

Bodies are never logged. The file lives in the daemon's state dir
(mode 600 in a 700 dir) — only the daemon and root can read.
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
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with _lock:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            str(log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
            0o600,
        )
        try:
            view = memoryview(data)
            written = 0
            while written < len(data):
                try:
                    n = os.write(fd, view[written:])
                except InterruptedError:
                    continue
                if n <= 0:
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
