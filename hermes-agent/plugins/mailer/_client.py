"""hermes-mailer client — thin UDS client.

This module is imported by Elena's mailer plugin. It does NOT import any
daemon-side modules (no policy, no transport, no credentials). It just
opens the socket, sends one JSON line, reads one JSON line, returns.

The client is responsible for the PATH side of attachment validation
(open-once, fstat S_ISREG, allowed-prefix, read exactly st.st_size). It
ships the resulting bytes to the daemon, which does the CONTENT side
(magic bytes, size caps, extension/MIME match).

This intentionally lives outside the `hermes_mailer` daemon package so
the package's daemon-only modules never accidentally end up on Elena's
sys.path.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOCKET_PATH = "/run/hermes-mailer/sock"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 32 * 1024  # responses are small (no attachments in response)


@dataclass(frozen=True)
class ClientError:
    """Raised inside the client layer (e.g. socket unreachable). The
    plugin shim catches these and surfaces them as `daemon_unreachable`
    in the tool's response."""

    reason: str
    detail: str = ""


class DaemonUnreachable(Exception):
    """Daemon socket not found / connect refused / read timeout."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _read_attachment_safely(raw_path: str, *, max_bytes: int,
                            allowed_prefixes: list[str]) -> tuple[str, bytes]:
    """Resolve + read an attachment from the local filesystem. Returns
    (basename, bytes). Raises ValueError with a stable token for any
    rejection — caller maps to invalid_input."""
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("attachment_invalid_path")
    p = Path(raw_path)
    if not p.is_absolute():
        raise ValueError("attachment_path_not_absolute")
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("attachment_not_found") from None
    except OSError:
        raise ValueError("attachment_resolve_failed") from None

    s_resolved = str(resolved)
    matched = False
    for prefix in allowed_prefixes:
        norm = prefix if prefix.endswith("/") else prefix + "/"
        if s_resolved == norm.rstrip("/") or s_resolved.startswith(norm):
            matched = True
            break
    if not matched:
        raise ValueError("attachment_outside_allowed_prefixes")

    try:
        pre_stat = os.lstat(s_resolved)
    except OSError:
        raise ValueError("attachment_stat_failed") from None
    if not stat.S_ISREG(pre_stat.st_mode):
        raise ValueError("attachment_not_regular_file")

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(s_resolved, flags)
    except OSError:
        raise ValueError("attachment_open_failed") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("attachment_not_regular_file")
        if st.st_size == 0:
            raise ValueError("attachment_empty")
        if st.st_size > max_bytes:
            raise ValueError("attachment_too_large")
        chunks = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != st.st_size:
            raise ValueError("attachment_short_read")
        extra = os.read(fd, 1)
        if extra:
            raise ValueError("attachment_changed_during_read")
    finally:
        os.close(fd)
    return (resolved.name, content)


def send(*, to: str, subject: str, body: str,
         body_html: str | None = None,
         attachment_paths: list[str] | None = None,
         allowed_prefixes: list[str] | None = None,
         max_attachment_bytes: int = 10 * 1024 * 1024,
         socket_path: str | None = None,
         timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Call the daemon. Returns the daemon's response dict verbatim.

    Raises DaemonUnreachable if the socket can't be reached or the daemon
    fails to respond within timeout_seconds. All other errors come back
    as response dicts with ok=False (handled per PROTOCOL.md).
    """
    sock_path = socket_path or os.environ.get(
        "HERMES_MAILER_SOCKET", DEFAULT_SOCKET_PATH)
    allowed_prefixes = allowed_prefixes or ["/tmp/"]

    request_id = uuid.uuid4().hex
    attachments_payload = []
    for raw_path in (attachment_paths or []):
        try:
            basename, content = _read_attachment_safely(
                raw_path,
                max_bytes=max_attachment_bytes,
                allowed_prefixes=allowed_prefixes,
            )
        except ValueError as e:
            # Construct a synthetic response so the caller sees the same
            # shape it would from the daemon.
            return {
                "v": 1, "request_id": request_id, "ok": False,
                "error": "invalid_input", "reason": str(e),
                "detail": os.path.basename(raw_path)[:80],
            }
        attachments_payload.append({
            "filename": basename,
            "content_b64": base64.b64encode(content).decode("ascii"),
        })

    envelope = {
        "v": 1,
        "op": "send",
        "request_id": request_id,
        "to": to,
        "subject": subject,
        "body": body,
        "body_html": body_html,
        "attachments": attachments_payload,
    }
    return _roundtrip(envelope, sock_path=sock_path, timeout_seconds=timeout_seconds)


def list_contacts(*, socket_path: str | None = None,
                  timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Ask the daemon for the caller's contact directory.

    Returns the daemon's response dict verbatim (contains a `contacts`
    list of {email, name, aliases, note, daily_limit, remaining_today}).
    Carries no attachments and no secrets — it's a read-only lookup the
    agent uses to resolve a name/alias to an allowlisted address.

    Raises DaemonUnreachable on socket trouble, same as `send`.
    """
    sock_path = socket_path or os.environ.get(
        "HERMES_MAILER_SOCKET", DEFAULT_SOCKET_PATH)
    envelope = {"v": 1, "op": "contacts", "request_id": uuid.uuid4().hex}
    return _roundtrip(envelope, sock_path=sock_path, timeout_seconds=timeout_seconds)


def _roundtrip(envelope: dict, *, sock_path: str,
               timeout_seconds: int) -> dict:
    """Open the UDS, write one JSON line, read one JSON line, close.

    Raises DaemonUnreachable if the socket can't be reached or the daemon
    fails to respond within timeout_seconds.
    """
    payload = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
               + "\n").encode("utf-8")

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_seconds)
        sock.connect(sock_path)
    except FileNotFoundError:
        raise DaemonUnreachable("socket_missing", sock_path)
    except ConnectionRefusedError:
        raise DaemonUnreachable("connect_refused", sock_path)
    except (TimeoutError, socket.timeout):
        raise DaemonUnreachable("connect_timeout", sock_path)
    except OSError as e:
        raise DaemonUnreachable("connect_failed", str(e))

    try:
        try:
            sock.sendall(payload)
        except (TimeoutError, socket.timeout, BrokenPipeError, OSError) as e:
            raise DaemonUnreachable("send_failed", str(e))

        # Read until newline.
        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except (TimeoutError, socket.timeout) as e:
                raise DaemonUnreachable("read_timeout", str(e))
            if not chunk:
                if not buf:
                    raise DaemonUnreachable("empty_response", "")
                break
            buf.extend(chunk)
            if len(buf) > MAX_RESPONSE_BYTES:
                raise DaemonUnreachable("response_too_large", str(len(buf)))
            if b"\n" in chunk:
                break
        line = bytes(buf).split(b"\n", 1)[0]
        try:
            return json.loads(line)
        except Exception as e:
            raise DaemonUnreachable("malformed_response", str(e))
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
