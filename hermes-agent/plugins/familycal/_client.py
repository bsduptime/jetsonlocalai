"""hermes-calendar client — thin UDS client.

Imported by the calendar plugin. Does NOT import any daemon-side modules
(no policy, no transport, no credentials). It opens the socket, sends one
JSON line, reads one JSON line, returns. Mirrors the mailer plugin's
`_client` but carries no attachments (calendar requests are tiny).
"""

from __future__ import annotations

import json
import os
import socket
import uuid

DEFAULT_SOCKET_PATH = "/run/hermes-calendar/sock"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 64 * 1024


class DaemonUnreachable(Exception):
    """Daemon socket not found / connect refused / read timeout."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def create_event(*, title: str, start: str, end: str | None,
                 duration_minutes: int | None, calendar: str | None,
                 location: str | None, notes: str | None,
                 attendees: list[dict],
                 socket_path: str | None = None,
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    envelope = {
        "v": 1,
        "op": "create_event",
        "request_id": uuid.uuid4().hex,
        "title": title,
        "start": start,
        "end": end,
        "duration_minutes": duration_minutes,
        "calendar": calendar,
        "location": location,
        "notes": notes,
        "attendees": attendees,
    }
    return _roundtrip(envelope, socket_path=socket_path,
                      timeout_seconds=timeout_seconds)


def list_contacts(*, socket_path: str | None = None,
                  timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    envelope = {"v": 1, "op": "contacts", "request_id": uuid.uuid4().hex}
    return _roundtrip(envelope, socket_path=socket_path,
                      timeout_seconds=timeout_seconds)


def _resolve_socket(socket_path: str | None) -> str:
    return (socket_path
            or os.environ.get("HERMES_CALENDAR_SOCKET")
            or DEFAULT_SOCKET_PATH)


def _connect(addr: str, timeout: int) -> socket.socket:
    """Open a stream socket to the relay. Supports a Unix path (Linux/macOS)
    or a localhost TCP address `tcp://host:port` (Windows / portable)."""
    if addr.startswith("tcp:"):
        hp = addr[6:] if addr.startswith("tcp://") else addr[4:]
        host, port = hp.rsplit(":", 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(((host or "127.0.0.1"), int(port)))
    else:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(addr)
    return s


def _roundtrip(envelope: dict, *, socket_path: str | None,
               timeout_seconds: int) -> dict:
    sock_path = _resolve_socket(socket_path)
    payload = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
               + "\n").encode("utf-8")

    try:
        sock = _connect(sock_path, timeout_seconds)
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
