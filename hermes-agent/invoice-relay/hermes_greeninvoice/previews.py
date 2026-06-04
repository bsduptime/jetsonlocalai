"""Spool preview PDFs to a shared file instead of returning base64 inline.

The GreenInvoice ``/documents/preview`` endpoint returns the rendered PDF as
a base64 string in ``result["file"]``. Shipping that back over the socket
would dump tens of KB of base64 into the agent's context on every draft (and
risk the client's response-size cap). Instead we decode it, write it to a
shared spool dir the ``hermes`` user can read, and replace the blob with a
file path the agent hands to its delivery channel (mailer / Telegram).

Spool dir: ``/run/hermes-greeninvoice/previews`` — tmpfs, under the unit's
RuntimeDirectory, so the daemon (DynamicUser) can write it. Files are chgrp'd
to ``hermes-greeninvoice-clients`` (the hermes user is a member) and mode
0640, so the agent can read them. Ephemeral: pruned by age + count and
cleared on reboot.
"""

from __future__ import annotations

import base64
import grp
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("hermes_greeninvoice.previews")

_CLIENTS_GROUP = "hermes-greeninvoice-clients"


def _chgrp_clients(path: str, mode: int) -> None:
    """Best-effort: hand group ownership to the clients group so the hermes
    user can read. No-op if the group doesn't exist (e.g. in tests)."""
    try:
        gid = grp.getgrnam(_CLIENTS_GROUP).gr_gid
        os.chown(path, -1, gid)
        os.chmod(path, mode)
    except (KeyError, OSError) as e:
        log.debug("could not chgrp %s to %s: %s", path, _CLIENTS_GROUP, e)


def ensure_dir(cfg) -> None:
    d = cfg.previews_dir
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o750)
        _chgrp_clients(str(d), 0o750)
    except OSError as e:
        log.warning("could not create previews dir %s: %s", d, e)


def _safe_name(request_id: str) -> str:
    keep = "".join(c for c in (request_id or "") if c.isalnum() or c in "_-")
    return keep[:64] or "preview"


def prune(cfg) -> None:
    """Drop previews older than the retention window, then cap the count."""
    d = cfg.previews_dir
    try:
        files = sorted(d.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    now = time.time()
    for p in list(files):
        try:
            if now - p.stat().st_mtime > cfg.preview_retention_seconds:
                p.unlink()
                files.remove(p)
        except OSError:
            pass
    excess = len(files) - cfg.preview_max_files
    for p in files[:max(0, excess)]:
        try:
            p.unlink()
        except OSError:
            pass


def spool(cfg, result, request_id: str):
    """If ``result`` carries a base64 ``file`` (a rendered preview PDF), write
    it to the spool dir and return a copy of ``result`` with ``file`` replaced
    by ``preview_pdf_path`` + ``preview_pdf_bytes``. Otherwise return ``result``
    unchanged. Never raises — on any failure we fall back to the original
    result (which still carries the inline base64)."""
    if not cfg.spool_previews or not isinstance(result, dict):
        return result
    b64 = result.get("file")
    if not isinstance(b64, str) or not b64:
        return result
    try:
        content = base64.b64decode(b64, validate=True)
    except Exception:
        return result  # not decodable — leave the original as-is
    try:
        ensure_dir(cfg)
        prune(cfg)
        path = cfg.previews_dir / f"{_safe_name(request_id)}.pdf"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        _chgrp_clients(str(path), 0o640)
    except OSError as e:
        log.warning("could not spool preview pdf: %s", e)
        return result
    out = {k: v for k, v in result.items() if k != "file"}
    out["preview_pdf_path"] = str(path)
    out["preview_pdf_bytes"] = len(content)
    return out
