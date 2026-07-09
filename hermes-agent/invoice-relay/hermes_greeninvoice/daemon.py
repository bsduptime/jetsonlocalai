"""hermes-greeninvoice daemon — UDS or loopback-TCP server.

For each connection:
  1. Resolve the caller identity:
       UDS  — connecting UID via peer credentials (SO_PEERCRED / LOCAL_PEERCRED).
       TCP  — a per-caller shared secret in the request envelope
              (`caller_token`, configured as GI_CALLER_TOKEN_<name>).
  2. Read up to MAX_REQUEST_BYTES with a wall-clock deadline.
  3. Parse the first line as a JSON request envelope.
  4. Dispatch to handler.handle.
  5. Write the JSON response + newline; close.

TCP mode exists for Windows, where CPython has no AF_UNIX: set
HERMES_GREENINVOICE_SOCKET=tcp://127.0.0.1:<port>. It binds loopback ONLY
and refuses every request unless at least one caller token is configured —
this broker holds live invoicing credentials, so no auth means no service.

One request per connection. The GreenInvoice API client (token cache +
throttle) is shared across connections and built lazily on first live use.
"""

from __future__ import annotations

import hmac
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
        if hasattr(socket, "SO_PEERCRED"):  # Linux: struct ucred {pid,uid,gid}
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_SIZE)
            if len(creds) < _UCRED_SIZE:
                return None
            _pid, uid, _gid = struct.unpack(_UCRED_FMT, creds)
            return uid
        if hasattr(socket, "LOCAL_PEERCRED"):  # macOS/BSD: struct xucred
            # {u_int cr_version; uid_t cr_uid; ...} — uid is the second u32.
            creds = conn.getsockopt(getattr(socket, "SOL_LOCAL", 0),
                                    socket.LOCAL_PEERCRED, 128)
            if len(creds) < 8:
                return None
            _version, uid = struct.unpack("2I", creds[:8])
            return uid
        return None
    except OSError:
        return None


def _resolve_token_caller(token: object, cfg: Config) -> str | None:
    """Map a TCP client's `caller_token` to a caller name. Compares against
    every configured token (constant-time per compare) so a miss costs the
    same as a hit."""
    if not isinstance(token, str) or not token:
        return None
    token_b = token.encode("utf-8")
    matched = None
    for configured, name in cfg.caller_token_map.items():
        if hmac.compare_digest(configured.encode("utf-8"), token_b):
            matched = name
    return matched


def _read_until_newline(conn: socket.socket, max_bytes: int,
                        deadline: float) -> tuple[bytes, bytes]:
    """Read the JSON header line. Returns (line, leftover) where `leftover` is
    any bytes already received past the newline (the start of a framed upload
    body). `max_bytes` bounds the header line only."""
    buf = bytearray()
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
            return bytes(buf), b""
        buf.extend(chunk)
        nl = buf.find(b"\n")
        if nl >= 0:
            if nl > max_bytes:
                raise ProtocolError("request_too_large", str(nl))
            return bytes(buf[:nl]), bytes(buf[nl + 1:])
        if len(buf) > max_bytes:
            raise ProtocolError("request_too_large", str(len(buf)))


def _read_exact(conn: socket.socket, need: int, initial: bytes,
                deadline: float) -> bytes:
    """Read exactly `need` bytes (the framed upload body), starting from any
    `initial` leftover. Rejects a client that sends more than it declared."""
    if len(initial) > need:
        raise ProtocolError("body_overrun", f"{len(initial)}>{need}")
    buf = bytearray(initial)
    while len(buf) < need:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("read_timeout", "deadline_exceeded")
        try:
            conn.settimeout(remaining)
            chunk = conn.recv(min(65536, need - len(buf)))
        except (TimeoutError, socket.timeout) as e:
            raise ProtocolError("read_timeout", str(e))
        if not chunk:
            raise ProtocolError("truncated_body", f"{len(buf)}/{need}")
        buf.extend(chunk)
    return bytes(buf)


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
        is_tcp = conn.family != getattr(socket, "AF_UNIX", None)
        caller: str | None = None
        if not is_tcp:
            # UDS: identity from kernel peer credentials, before reading a byte.
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

        deadline = time.monotonic() + CONN_DEADLINE_SECONDS
        try:
            raw, leftover = _read_until_newline(conn, cfg.max_request_bytes, deadline)
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

        if is_tcp:
            # TCP: identity from the envelope's caller_token. Fail closed —
            # with no tokens configured, TCP mode serves nobody.
            token = req.pop("caller_token", None)
            if not cfg.caller_token_map:
                _write_response(conn, {"v": 1, "request_id": request_id, "ok": False,
                                       "error": "protocol",
                                       "reason": "tcp_auth_not_configured",
                                       "detail": "set GI_CALLER_TOKEN_<name> on the daemon"})
                return
            caller = _resolve_token_caller(token, cfg)
            if caller is None:
                audit.append(cfg.audit_log_path, {"caller": "tcp_unknown",
                                                  "outcome": "deny",
                                                  "reason": "unknown_caller"})
                _write_response(conn, {"v": 1, "request_id": request_id, "ok": False,
                                       "error": "protocol", "reason": "unknown_caller",
                                       "detail": "missing or unrecognized caller_token"})
                return

        # Framed upload: upload_expense_file carries `byte_len` raw bytes after
        # the header line. Read exactly that many (bounded), else drop the
        # leftover. The daemon reads the body even in dry-run to keep framing.
        file_body = None
        if req.get("op") == "upload_expense_file":
            uargs = req.get("args") if isinstance(req.get("args"), dict) else {}
            byte_len = uargs.get("byte_len")
            if isinstance(byte_len, bool) or not isinstance(byte_len, int) \
                    or byte_len <= 0 or byte_len > cfg.max_upload_file_bytes:
                _write_response(conn, {"v": 1, "request_id": request_id, "ok": False,
                                       "error": "protocol", "reason": "invalid_upload_length",
                                       "detail": str(byte_len)[:40]})
                return
            try:
                file_body = _read_exact(conn, byte_len, leftover, deadline)
            except ProtocolError as e:
                _write_response(conn, {"v": 1, "request_id": request_id, "ok": False,
                                       "error": "protocol", "reason": e.reason,
                                       "detail": e.detail})
                return

        response = handle(cfg=cfg, caller=caller, request=req,
                          get_client=clients.get, file_body=file_body)
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


def _is_tcp_addr(addr: Path | str) -> bool:
    return isinstance(addr, str) and addr.startswith("tcp:")


def _parse_tcp_addr(addr: str) -> tuple[str, int]:
    hp = addr[6:] if addr.startswith("tcp://") else addr[4:]
    host, port = hp.rsplit(":", 1)
    return (host or "127.0.0.1"), int(port)


def _bind_socket(path: Path | str) -> socket.socket:
    if _is_tcp_addr(path):
        host, port = _parse_tcp_addr(str(path))
        # TCP identity is only a shared token — never expose that beyond
        # loopback. A routable bind would offer the invoice broker (live
        # GreenInvoice credentials behind it) to the whole network.
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"refusing to bind greeninvoice broker TCP to non-loopback host "
                f"{host!r}; use 127.0.0.1"
            )
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: stop another local process from stealing the port.
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(MAX_CONCURRENT * 2)
        server.settimeout(1.0)
        return server

    if not hasattr(socket, "AF_UNIX"):
        raise OSError(
            "this platform has no AF_UNIX — set "
            "HERMES_GREENINVOICE_SOCKET=tcp://127.0.0.1:<port> and configure "
            "GI_CALLER_TOKEN_<name> for each client"
        )
    path = Path(path)
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
