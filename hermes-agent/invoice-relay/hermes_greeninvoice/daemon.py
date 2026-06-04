"""hermes-greeninvoice daemon — UDS server.

For each connection:
  1. Resolve the connecting UID via SO_PEERCRED -> caller identity.
  2. Read up to MAX_REQUEST_BYTES with a wall-clock deadline.
  3. Parse the first line as a JSON request envelope.
  4. Dispatch to handler.handle.
  5. Write the JSON response + newline; close.

One request per connection. The GreenInvoice API client (token cache +
throttle) is shared across connections and built lazily on first live use.
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

from . import audit, previews, ratelimit
from .apiclient import GreenInvoiceClient
from .config import Config, ensure_state_dirs, load_config
from .errors import ProtocolError, UpstreamError
from .handler import handle

log = logging.getLogger("hermes_greeninvoice.daemon")

# SO_PEERCRED: struct ucred { pid_t pid; uid_t uid; gid_t gid; } — 3×u32.
_UCRED_FMT = "3i"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)

CONN_DEADLINE_SECONDS = 35   # a touch above the API HTTP timeout (30s)
MAX_CONCURRENT = 4
_in_flight = threading.BoundedSemaphore(MAX_CONCURRENT)


class _ClientHolder:
    """Lazily builds and caches one shared GreenInvoiceClient. Thread-safe."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._client: GreenInvoiceClient | None = None

    def get(self) -> GreenInvoiceClient:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                self._client = GreenInvoiceClient(self._cfg)
            return self._client


def _resolve_caller(uid: int, cfg: Config) -> str | None:
    if uid in cfg.caller_uid_map:
        return cfg.caller_uid_map[uid]
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return None
    if name == "hermes" and "hermes" not in cfg.caller_uid_map.values():
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


def _handle_connection(conn: socket.socket, cfg: Config, clients: _ClientHolder) -> None:
    request_id = ""
    try:
        conn.settimeout(CONN_DEADLINE_SECONDS)
        uid = _peercred_uid(conn)
        if uid is None:
            _write_response(conn, {"v": 1, "request_id": "", "ok": False,
                                   "error": "protocol", "reason": "peercred_unavailable"})
            return
        caller = _resolve_caller(uid, cfg)
        if caller is None:
            audit.append(cfg.audit_log_path, {"caller": f"uid_{uid}",
                                              "outcome": "deny", "reason": "unknown_caller"})
            _write_response(conn, {"v": 1, "request_id": "", "ok": False,
                                   "error": "protocol", "reason": "unknown_caller",
                                   "detail": f"uid={uid}"})
            return

        try:
            raw = _read_until_newline(conn, cfg.max_request_bytes)
        except ProtocolError as e:
            _write_response(conn, {"v": 1, "request_id": "", "ok": False,
                                   "error": "protocol", "reason": e.reason, "detail": e.detail})
            return

        try:
            req = json.loads(raw)
        except Exception:
            _write_response(conn, {"v": 1, "request_id": "", "ok": False,
                                   "error": "protocol", "reason": "malformed_json"})
            return

        if not isinstance(req, dict):
            _write_response(conn, {"v": 1, "request_id": "", "ok": False,
                                   "error": "protocol", "reason": "request_not_object"})
            return

        request_id = req.get("request_id", "") if isinstance(req.get("request_id"), str) else ""

        if req.get("v") != 1:
            _write_response(conn, {"v": 1, "request_id": request_id, "ok": False,
                                   "error": "protocol", "reason": "version_mismatch",
                                   "detail": f"server=1 got={req.get('v')!r}"})
            return

        response = handle(cfg=cfg, caller=caller, request=req, get_client=clients.get)
        _write_response(conn, response)
    except Exception as e:
        log.exception("internal error handling connection")
        _write_response(conn, {"v": 1, "request_id": request_id, "ok": False,
                               "error": "protocol", "reason": "internal_error",
                               "detail": type(e).__name__})
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def _accept_loop(server: socket.socket, cfg: Config, clients: _ClientHolder,
                 stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            conn, _ = server.accept()
        except OSError:
            if stop.is_set():
                return
            continue
        if not _in_flight.acquire(blocking=False):
            _write_response(conn, {"v": 1, "request_id": "", "ok": False,
                                   "error": "protocol", "reason": "server_busy"})
            conn.close()
            continue

        def _worker(c=conn):
            try:
                _handle_connection(c, cfg, clients)
            finally:
                _in_flight.release()

        threading.Thread(target=_worker, daemon=True).start()


def _bind_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(str(path), 0o660)
    try:
        import grp
        gid = grp.getgrnam("hermes-greeninvoice-clients").gr_gid
        os.chown(str(path), -1, gid)
        try:
            os.chown(str(path.parent), -1, gid)
            os.chmod(str(path.parent), 0o750)
        except OSError as e:
            log.warning("could not retag runtime dir: %s", e)
    except (KeyError, OSError) as e:
        log.warning("could not chgrp socket to hermes-greeninvoice-clients (%s); "
                    "clients may be unable to connect. Run setup-hermes-greeninvoice.sh.", e)
    server.listen(MAX_CONCURRENT * 2)
    server.settimeout(1.0)
    return server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("HERMES_GREENINVOICE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config()
    ensure_state_dirs(cfg)
    previews.ensure_dir(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)
    ratelimit.reap_stale_reservations(cfg.ratelimit_db_path, cfg.reservation_ttl_seconds)

    log.info("hermes-greeninvoice starting: env=%s dry_run=%s socket=%s state=%s",
             cfg.env, cfg.dry_run, cfg.socket_path, cfg.state_dir)
    if not cfg.dry_run and (not cfg.api_key_id or not cfg.api_key_secret):
        log.error("GI_DRY_RUN=false but GI_API_KEY_ID/SECRET unset — live ops will fail")

    clients = _ClientHolder(cfg)
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

    _accept_loop(server, cfg, clients, stop)
    log.info("hermes-greeninvoice stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
