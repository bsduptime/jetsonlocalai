"""hermes-calendar daemon: UDS server + request dispatch.

`handle_request` is pure-ish (side effects only via the injected transport
+ audit) so the demo and tests can call it directly. `run_daemon` wraps it
in a Unix-socket accept loop that speaks the one-JSON-line-per-connection
protocol the plugin's `_client` uses (see PROTOCOL.md).
"""

from __future__ import annotations

import json
import os
import socket
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import grp  # POSIX-only; used to group-gate the socket on Linux (the Jetson)
except ImportError:  # Windows / non-POSIX (e.g. a client's Windows mini PC)
    grp = None

from . import event as event_mod
from .transport import DryRunTransport, TransportError, make_transport

PROTOCOL_VERSION = 1


def _err(request_id: str, error: str, reason: str, detail: str = "") -> dict:
    return {"v": PROTOCOL_VERSION, "request_id": request_id, "ok": False,
            "error": error, "reason": reason, "detail": detail}


def _today_str(tz: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _ratelimit_check_and_consume(cfg, *, consume: bool) -> bool:
    """Returns True if allowed. Only real (non-dry-run) writes consume quota."""
    if not consume:
        return True
    path = Path(cfg.state_dir) / "ratelimit.json"
    today = _today_str(cfg.tz)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    if data["count"] >= cfg.daily_limit:
        return False
    data["count"] += 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass
    return True


def _audit(cfg, request_id: str, plan: dict, dry_run: bool) -> None:
    try:
        line = {
            "ts": datetime.now(ZoneInfo(cfg.tz)).isoformat(),
            "request_id": request_id,
            "dry_run": dry_run,
            "title": plan["event"]["title"],
            "start": plan["event"]["start"],
            "calendar": plan["calendar"]["label"],
            "invited": [e["email"] for e in plan["invited"]],
            "informed": [e["email"] for e in plan["informed"]],
        }
        p = Path(cfg.state_dir)
        p.mkdir(parents=True, exist_ok=True)
        with (p / "events.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass  # audit is best-effort; never fail the request on logging


def handle_request(envelope: dict, cfg, *, transport=None) -> dict:
    request_id = str(envelope.get("request_id") or "")
    op = envelope.get("op")

    if op == "contacts":
        return {
            "v": PROTOCOL_VERSION, "request_id": request_id, "ok": True,
            "contacts": [
                {"email": c.get("email") or None, "name": c.get("name"),
                 "aliases": c.get("aliases") or [],
                 "default_role": c.get("default_role"), "note": c.get("note")}
                for c in cfg.contacts
            ],
        }

    if op != "create_event":
        return _err(request_id, "invalid_input", "unknown_op", str(op)[:40])

    if not envelope.get("title") or not envelope.get("start"):
        return _err(request_id, "invalid_input", "missing_required_field")

    try:
        plan = event_mod.build_plan(envelope, cfg)
    except event_mod.PlanError as e:
        return _err(request_id, "invalid_input", str(e))

    if transport is None:
        try:
            transport = make_transport(cfg)
        except TransportError as e:
            return _err(request_id, "transport_failed", e.reason, e.detail)

    dry_run = isinstance(transport, DryRunTransport)
    if not _ratelimit_check_and_consume(cfg, consume=not dry_run):
        return _err(request_id, "not_allowed", "rate_limit_exceeded",
                    f"daily_limit={cfg.daily_limit}")

    try:
        result = transport.execute(plan, request_id=request_id)
    except TransportError as e:
        return _err(request_id, "transport_failed", e.reason, e.detail)

    _audit(cfg, request_id, plan, dry_run)

    return {
        "v": PROTOCOL_VERSION, "request_id": request_id, "ok": True,
        "dry_run": result.get("dry_run", dry_run),
        "calendar": plan["calendar"],
        "event": plan["event"],
        "invited": plan["invited"],
        "informed": plan["informed"],
        "unresolved": plan["unresolved"],
        "warnings": plan["warnings"],
        "calendar_url": result.get("calendar_url"),
        "event_id": result.get("event_id"),
        "summary": event_mod.render_summary(plan, dry_run=dry_run),
    }


# --------------------------------------------------------------------------
# UDS daemon
# --------------------------------------------------------------------------

def _read_line(conn: socket.socket, max_bytes: int = 256 * 1024) -> bytes:
    buf = bytearray()
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            break
        if b"\n" in chunk:
            break
    return bytes(buf).split(b"\n", 1)[0]


def _serve_socket(sock: socket.socket, cfg) -> None:
    transport = None  # lazily created; dry-run transport is reusable
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            break
        try:
            conn.settimeout(30)
            line = _read_line(conn)
            try:
                envelope = json.loads(line) if line else {}
            except ValueError:
                resp = _err("", "invalid_input", "malformed_request")
            else:
                if transport is None:
                    try:
                        transport = make_transport(cfg)
                    except TransportError:
                        transport = None  # recompute per-request if it failed
                resp = handle_request(envelope, cfg, transport=transport)
            payload = (json.dumps(resp, ensure_ascii=False,
                                  separators=(",", ":")) + "\n").encode("utf-8")
            conn.sendall(payload)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()


def _is_tcp(addr: str) -> bool:
    return addr.startswith("tcp:")


def _parse_tcp(addr: str) -> tuple[str, int]:
    hp = addr[6:] if addr.startswith("tcp://") else addr[4:]
    host, port = hp.rsplit(":", 1)
    return (host or "127.0.0.1"), int(port)


def make_server_socket(socket_path: str) -> socket.socket:
    # Portable path (Windows, or anywhere without Unix sockets): bind a
    # localhost TCP port instead. Set HERMES_CALENDAR_SOCKET=tcp://127.0.0.1:8765.
    # NOTE: TCP has no group-gating — bind 127.0.0.1 only and rely on the host
    # firewall. Fine for a single-family mini PC; on Linux prefer the UDS.
    if _is_tcp(socket_path):
        host, port = _parse_tcp(socket_path)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(16)
        return sock

    Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_ISSOCK(os.stat(socket_path).st_mode):
            os.unlink(socket_path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    try:
        os.chmod(socket_path, 0o660)
    except OSError:
        pass
    # Lock the socket to a "clients" group so only its members (the hermes
    # user / Elena) can connect() — mirrors mail-relay. Under DynamicUser the
    # daemon's primary group is transient, so we chgrp to the stable clients
    # group the daemon holds as a supplementary group. No-op if unset (dev).
    clients_group = os.environ.get("HERMES_CALENDAR_CLIENTS_GROUP")
    if clients_group and grp is not None:
        try:
            gid = grp.getgrnam(clients_group).gr_gid
            os.chown(socket_path, -1, gid)
            os.chmod(socket_path, 0o660)
            # Retag the runtime dir so peeking requires group membership too.
            parent = str(Path(socket_path).parent)
            os.chown(parent, -1, gid)
            os.chmod(parent, 0o750)
        except (KeyError, OSError):
            pass  # group missing / not permitted -> fall back to owner-only
    sock.listen(16)
    return sock


def run_daemon(cfg) -> None:
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    sock = make_server_socket(cfg.socket_path)
    mode = "DRY-RUN" if cfg.dry_run else "LIVE"
    print(f"hermes-calendar [{mode}] listening on {cfg.socket_path} "
          f"(tz={cfg.tz}, contacts={len(cfg.contacts)})", flush=True)
    try:
        _serve_socket(sock, cfg)
    finally:
        sock.close()
        try:
            os.unlink(cfg.socket_path)
        except OSError:
            pass


def main() -> None:  # pragma: no cover - entry point
    from .config import load_config
    cfg = load_config()
    run_daemon(cfg)


if __name__ == "__main__":  # pragma: no cover
    main()
