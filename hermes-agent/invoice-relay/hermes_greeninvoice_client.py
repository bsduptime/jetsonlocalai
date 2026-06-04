"""hermes-greeninvoice client — thin UDS client.

Imported by Hermes' greeninvoice plugin. Does NOT import any daemon-side
module (no policy, no credentials, no API client). It opens the socket,
writes one JSON line, reads one JSON line, returns the dict.

Lives outside the `hermes_greeninvoice` package on purpose, so the
package's credential-bearing daemon modules never end up on Hermes'
sys.path.
"""

from __future__ import annotations

import json
import os
import socket
import uuid

DEFAULT_SOCKET_PATH = "/run/hermes-greeninvoice/sock"
DEFAULT_TIMEOUT_SECONDS = 40
MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # search results can be a few KB; cap at 1MiB


class DaemonUnreachable(Exception):
    """Socket not found / connect refused / read timeout / malformed reply."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def call(op: str, args: dict | None = None, *,
         socket_path: str | None = None,
         timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Send one `{v, op, request_id, args}` envelope to the daemon and
    return its response dict verbatim. Raises DaemonUnreachable on socket
    trouble; all policy errors come back as ok=False dicts."""
    sock_path = socket_path or os.environ.get(
        "HERMES_GREENINVOICE_SOCKET", DEFAULT_SOCKET_PATH)
    envelope = {
        "v": 1,
        "op": op,
        "request_id": uuid.uuid4().hex,
        "args": args or {},
    }
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
