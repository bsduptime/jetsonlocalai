"""mailer plugin handler — thin shim over the hermes-mailer daemon.

This module replaces the previous in-process implementation. All policy
(allowlist, rate-limit, transport, magic-byte validation, etc.) now lives
in a separate `hermes-mailer.service` running as a transient DynamicUser.
Elena's process talks to the daemon over a Unix socket and never sees
the Resend / SMTP credentials.

Contract (unchanged from the agent's perspective):
  - Same input fields (to, subject, body, body_html, attachments).
  - Same response shapes (PROTOCOL.md mirrors the originals).
  - Always returns a JSON string; never raises.

What the handler does:
  1. Validate field types up-front so simple LLM mistakes don't even
     reach the socket.
  2. Hand the request to `_client.send(...)`, which:
       a. resolves + reads each attachment from disk on Elena's side
          (path-level validation stays in Elena's process — the daemon
          never sees a file path);
       b. opens a UDS connection to the daemon;
       c. ships a JSON envelope; reads a JSON response.
  3. If the daemon is unreachable, return a structured
     `transport_failed / daemon_unreachable` response.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import _client


def send_email(args: dict, **_kwargs) -> str:
    try:
        to = args.get("to")
        subject = args.get("subject")
        body = args.get("body")
        body_html = args.get("body_html") or None
        raw_atts = args.get("attachments") or []
    except Exception as e:
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "args_unreadable", "detail": type(e).__name__,
        })

    # Client-side attachment-path config. We deliberately read these env
    # vars here rather than configuring the daemon — they describe Elena's
    # filesystem, not the daemon's.
    allowed_prefixes = [p.strip() for p in (
        os.environ.get("EMAIL_ATTACHMENT_ALLOWED_PREFIXES") or "/tmp/"
    ).split(":") if p.strip()]
    try:
        max_attachment_bytes = int(
            os.environ.get("EMAIL_MAX_ATTACHMENT_BYTES") or (10 * 1024 * 1024)
        )
    except (TypeError, ValueError):
        max_attachment_bytes = 10 * 1024 * 1024

    if not isinstance(raw_atts, list):
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "invalid_field_type", "detail": "attachments",
        })
    # Reject obvious non-string paths early so we don't pass garbage.
    for i, p in enumerate(raw_atts):
        if not isinstance(p, str):
            return json.dumps({
                "ok": False, "error": "invalid_input",
                "reason": "attachment_invalid_path",
                "detail": f"attachments[{i}]",
            })

    try:
        resp = _client.send(
            to=to or "",
            subject=subject or "",
            body=body or "",
            body_html=body_html,
            attachment_paths=raw_atts,
            allowed_prefixes=allowed_prefixes,
            max_attachment_bytes=max_attachment_bytes,
        )
    except _client.DaemonUnreachable as e:
        return json.dumps({
            "ok": False, "error": "transport_failed",
            "reason": "daemon_unreachable", "detail": f"{e.reason}: {e.detail}",
        })
    except Exception as e:
        # Last-resort safety net — handler MUST always return JSON.
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "internal_error", "detail": type(e).__name__,
        })

    # Strip the protocol-version key the daemon sends back (the agent
    # doesn't need to know about that). Keep everything else.
    if isinstance(resp, dict) and "v" in resp:
        resp = {k: v for k, v in resp.items() if k != "v"}
    return json.dumps(resp)


def list_contacts(args: dict, **_kwargs) -> str:
    """Return the pre-approved contact directory so the agent can resolve a
    name/alias (e.g. "yoram", "my email") to an allowlisted address before
    calling send_email. Read-only; sends nothing. Always returns JSON,
    never raises."""
    try:
        resp = _client.list_contacts()
    except _client.DaemonUnreachable as e:
        return json.dumps({
            "ok": False, "error": "transport_failed",
            "reason": "daemon_unreachable", "detail": f"{e.reason}: {e.detail}",
        })
    except Exception as e:
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "internal_error", "detail": type(e).__name__,
        })
    if isinstance(resp, dict) and "v" in resp:
        resp = {k: v for k, v in resp.items() if k != "v"}
    return json.dumps(resp)
