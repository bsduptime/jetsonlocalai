"""hermes-mailer daemon — UDS server.

Listens on a Unix domain socket. For each connection:
  1. Reads up to MAX_REQUEST_BYTES with a deadline.
  2. Parses the first line as a JSON request envelope.
  3. Resolves the connecting UID via SO_PEERCRED -> caller identity.
  4. Dispatches to handler.handle_send.
  5. Writes the JSON response + newline.
  6. Closes the connection.

One request per connection — keep the protocol stupid simple. The cost
is one fd setup per request, which is negligible at our send rates.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import audit, ratelimit
from .config import Config, ensure_state_dirs, load_config
from .errors import ProtocolError
from .handler import handle_contacts, handle_send

log = logging.getLogger("hermes_mailer.daemon")

# SO_PEERCRED structure: struct ucred { pid_t pid; uid_t uid; gid_t gid; }
# All three are u32 on Linux.
_UCRED_FMT = "3i"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)

# Per-connection deadline, hard wall-clock.
CONN_DEADLINE_SECONDS = 60

# Concurrent connections cap. Set conservatively: each in-flight request
# can hold up to ~36 MiB raw + ~27 MiB decoded base64 + ~27 MiB validated
# bytes simultaneously. 4 × ~90 MiB peak ≈ 360 MiB, fits under the unit's
# MemoryMax=512M with Python overhead.
MAX_CONCURRENT = 4
_in_flight = threading.BoundedSemaphore(MAX_CONCURRENT)


# ---------------------------------------------------------------------------

def _resolve_caller(uid: int, cfg: Config) -> str | None:
    """Map a connecting UID -> caller identity. Returns None for unknown."""
    if uid in cfg.caller_uid_map:
        return cfg.caller_uid_map[uid]
    # If nothing's configured, try a username lookup; only "hermes" maps
    # to "elena" as the default. Anything else: unknown.
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return None
    if name == "hermes" and "hermes" not in cfg.caller_uid_map.values():
        # explicit configuration overrides this default
        return "elena"
    return None


def _peercred_uid(conn: socket.socket) -> int | None:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_SIZE)
        if len(creds) < _UCRED_SIZE:
            return None
        _pid, uid, _gid = struct.unpack(_UCRED_FMT, creds)
        return uid
    except OSError:
        return None


def _read_until_newline(conn: socket.socket, max_bytes: int,
                         deadline_seconds: float = CONN_DEADLINE_SECONDS) -> bytes:
    """Read from conn until \\n or max_bytes (whichever first). Raises
    ProtocolError on overflow or read failure.

    Enforces a WALL-CLOCK deadline using time.monotonic(). The earlier
    version called `conn.settimeout(CONN_DEADLINE_SECONDS)` once before
    this function, but socket timeouts apply per-recv — a slow-trickle
    client (1 byte every 59 s) would never trigger the timeout and could
    hold a worker slot indefinitely. With MAX_CONCURRENT=4, four such
    clients would starve all legitimate traffic. The monotonic deadline
    here closes that vector.
    """
    buf = bytearray()
    deadline = time.monotonic() + deadline_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("read_timeout", "deadline_exceeded")
        try:
            conn.settimeout(remaining)
            chunk = conn.recv(4096)
        except (TimeoutError, socket.timeout) as e:
            raise ProtocolError("read_timeout", str(e))
        if not chunk:
            if not buf:
                raise ProtocolError("empty_request", "")
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ProtocolError("request_too_large", str(len(buf)))
        nl = buf.find(b"\n")
        if nl >= 0:
            return bytes(buf[:nl])


def _write_response(conn: socket.socket, payload: dict) -> None:
    try:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        conn.sendall(line.encode("utf-8"))
    except OSError:
        pass


def _handle_connection(conn: socket.socket, cfg: Config) -> None:
    request_id = ""
    try:
        # Initial timeout — _read_until_newline overrides this with a
        # wall-clock deadline. Setting a sane non-blocking default in
        # case any later code does a raw recv before the deadline path.
        conn.settimeout(CONN_DEADLINE_SECONDS)
        uid = _peercred_uid(conn)
        if uid is None:
            _write_response(conn, {
                "v": 1, "request_id": "", "ok": False,
                "error": "protocol", "reason": "peercred_unavailable",
            })
            return
        caller = _resolve_caller(uid, cfg)
        if caller is None:
            audit.append(cfg.audit_log_path, {
                "request_id": "", "caller": f"uid_{uid}",
                "event": "deny", "outcome": "unknown_caller",
            })
            _write_response(conn, {
                "v": 1, "request_id": "", "ok": False,
                "error": "protocol", "reason": "unknown_caller",
                "detail": f"uid={uid}",
            })
            return

        try:
            raw = _read_until_newline(conn, cfg.max_request_bytes)
        except ProtocolError as e:
            _write_response(conn, {
                "v": 1, "request_id": "", "ok": False,
                "error": "protocol", "reason": e.reason, "detail": e.detail,
            })
            return

        try:
            req = json.loads(raw)
        except Exception:
            _write_response(conn, {
                "v": 1, "request_id": "", "ok": False,
                "error": "protocol", "reason": "malformed_json",
            })
            return

        if not isinstance(req, dict):
            _write_response(conn, {
                "v": 1, "request_id": "", "ok": False,
                "error": "protocol", "reason": "request_not_object",
            })
            return

        request_id = req.get("request_id", "") if isinstance(req.get("request_id"), str) else ""

        if req.get("v") != 1:
            _write_response(conn, {
                "v": 1, "request_id": request_id, "ok": False,
                "error": "protocol", "reason": "version_mismatch",
                "detail": f"server=1 got={req.get('v')!r}",
            })
            return

        op = req.get("op")
        if op == "send":
            response = handle_send(cfg=cfg, caller=caller, request=req)
        elif op == "contacts":
            response = handle_contacts(cfg=cfg, caller=caller, request=req)
        else:
            _write_response(conn, {
                "v": 1, "request_id": request_id, "ok": False,
                "error": "protocol", "reason": "unknown_op",
                "detail": str(op)[:40],
            })
            return

        _write_response(conn, response)
    except Exception as e:
        # Last-resort safety net. Never raise out to the accept loop.
        log.exception("internal error handling connection")
        _write_response(conn, {
            "v": 1, "request_id": request_id, "ok": False,
            "error": "protocol", "reason": "internal_error",
            "detail": type(e).__name__,
        })
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def _accept_loop(server: socket.socket, cfg: Config, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            conn, _ = server.accept()
        except OSError:
            if stop.is_set():
                return
            continue
        if not _in_flight.acquire(blocking=False):
            # Over the concurrency cap — reject immediately.
            _write_response(conn, {
                "v": 1, "request_id": "", "ok": False,
                "error": "protocol", "reason": "server_busy",
            })
            conn.close()
            continue

        def _worker(c=conn):
            try:
                _handle_connection(c, cfg)
            finally:
                _in_flight.release()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


def _bind_socket(path: Path) -> socket.socket:
    # systemd typically pre-creates the runtime dir via RuntimeDirectory=.
    # If it didn't, create it (mode 0750).
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    # If a stale socket exists, unlink it (no-op if it doesn't exist).
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    # Lock the socket to mode 0660 owned by group `hermes-mailer-clients`,
    # so only members of that group (hermes among them) can connect().
    # DynamicUser=yes gives the daemon a transient primary group; we need
    # the SUP group ownership instead. The daemon must be in
    # hermes-mailer-clients (via SupplementaryGroups in the unit) for
    # chgrp to succeed.
    os.chmod(str(path), 0o660)
    try:
        import grp
        gid = grp.getgrnam("hermes-mailer-clients").gr_gid
        os.chown(str(path), -1, gid)
        # Also retag the runtime dir so peeking at it requires group membership.
        try:
            os.chown(str(path.parent), -1, gid)
            os.chmod(str(path.parent), 0o750)
        except OSError as e:
            log.warning("could not retag runtime dir: %s", e)
    except (KeyError, OSError) as e:
        log.warning(
            "could not chgrp socket to hermes-mailer-clients (%s); "
            "clients may be unable to connect. Run setup-hermes-mailer.sh.",
            e,
        )
    server.listen(MAX_CONCURRENT * 2)
    server.settimeout(1.0)  # so the accept loop can check stop.is_set()
    return server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("HERMES_MAILER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_config()
    ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)
    ratelimit.reap_stale_reservations(cfg.ratelimit_db_path, cfg.reservation_ttl_seconds)

    log.info("hermes-mailer starting: socket=%s state_dir=%s",
             cfg.socket_path, cfg.state_dir)

    server = _bind_socket(cfg.socket_path)

    stop = threading.Event()

    def _on_signal(_signum, _frame):
        log.info("shutdown signal received")
        stop.set()
        try:
            server.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    _accept_loop(server, cfg, stop)

    log.info("hermes-mailer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
