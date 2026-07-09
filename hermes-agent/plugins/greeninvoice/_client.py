"""hermes-greeninvoice client — thin UDS/TCP client (plugin-local copy).

Deployed copy of invoice-relay/hermes_greeninvoice_client.py. Imported by
the plugin handler. Does NOT import any daemon-side module — no policy, no
credentials. Opens the socket, writes one JSON line, reads one JSON line.
"""

from __future__ import annotations

import json
import os
import socket
import uuid

DEFAULT_SOCKET_PATH = "/run/hermes-greeninvoice/sock"
DEFAULT_TIMEOUT_SECONDS = 40
# File uploads (get presigned URL + POST to S3) take longer than a JSON op.
UPLOAD_TIMEOUT_SECONDS = 90
MAX_RESPONSE_BYTES = 1 * 1024 * 1024


class DaemonUnreachable(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _connect(sock_path: str, timeout_seconds: int) -> socket.socket:
    try:
        if sock_path.startswith("tcp:"):
            hp = sock_path[6:] if sock_path.startswith("tcp://") else sock_path[4:]
            host, port = hp.rsplit(":", 1)
            return socket.create_connection(
                (host or "127.0.0.1", int(port)), timeout=timeout_seconds)
        if not hasattr(socket, "AF_UNIX"):
            raise DaemonUnreachable(
                "af_unix_unsupported",
                "set HERMES_GREENINVOICE_SOCKET=tcp://127.0.0.1:<port>")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_seconds)
        sock.connect(sock_path)
        return sock
    except FileNotFoundError:
        raise DaemonUnreachable("socket_missing", sock_path)
    except ConnectionRefusedError:
        raise DaemonUnreachable("connect_refused", sock_path)
    except (TimeoutError, socket.timeout):
        raise DaemonUnreachable("connect_timeout", sock_path)
    except OSError as e:
        raise DaemonUnreachable("connect_failed", str(e))


def _auth_fields() -> dict:
    """Over TCP the daemon identifies us by a shared secret, not peer
    credentials — attach it when configured. Harmless over UDS (ignored)."""
    token = os.environ.get("HERMES_GREENINVOICE_TOKEN", "").strip()
    return {"caller_token": token} if token else {}


def _recv_response(sock: socket.socket) -> dict:
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


def call(op: str, args: dict | None = None, *,
         socket_path: str | None = None,
         timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    sock_path = socket_path or os.environ.get(
        "HERMES_GREENINVOICE_SOCKET", DEFAULT_SOCKET_PATH)
    envelope = {
        "v": 1,
        "op": op,
        "request_id": uuid.uuid4().hex,
        "args": args or {},
        **_auth_fields(),
    }
    payload = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
               + "\n").encode("utf-8")

    sock = _connect(sock_path, timeout_seconds)
    try:
        try:
            sock.sendall(payload)
        except (TimeoutError, socket.timeout, BrokenPipeError, OSError) as e:
            raise DaemonUnreachable("send_failed", str(e))
        return _recv_response(sock)
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def call_with_file(op: str, args: dict, file_bytes: bytes, *,
                   socket_path: str | None = None,
                   timeout_seconds: int = UPLOAD_TIMEOUT_SECONDS) -> dict:
    """Framed upload: send the JSON header line (with byte_len) then the raw
    file bytes, and read one JSON response. Used by upload_expense_file."""
    sock_path = socket_path or os.environ.get(
        "HERMES_GREENINVOICE_SOCKET", DEFAULT_SOCKET_PATH)
    hdr = dict(args or {})
    hdr["byte_len"] = len(file_bytes)
    envelope = {
        "v": 1,
        "op": op,
        "request_id": uuid.uuid4().hex,
        "args": hdr,
        **_auth_fields(),
    }
    header = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
              + "\n").encode("utf-8")

    sock = _connect(sock_path, timeout_seconds)
    try:
        try:
            sock.sendall(header)
            sock.sendall(file_bytes)
        except (TimeoutError, socket.timeout, BrokenPipeError, OSError) as e:
            raise DaemonUnreachable("send_failed", str(e))
        return _recv_response(sock)
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
