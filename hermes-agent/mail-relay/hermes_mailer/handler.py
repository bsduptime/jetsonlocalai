"""Request handler — the policy enforcement core.

Called by the daemon for each incoming request. Returns a dict that the
daemon serializes back to the client. NEVER raises; all errors are
converted to response dicts per PROTOCOL.md.
"""

from __future__ import annotations

import base64
from typing import Any

from . import allowlist, attachments, audit, ratelimit
from .config import Config
from .errors import InvalidInput, NotAllowed, PreSendError
from .headers import (
    address_only,
    validate_address_field,
    validate_body,
    validate_subject,
)
from .transport import RenderedEmail, make_transport


def _err(*, error: str, reason: str, request_id: str, **extra) -> dict:
    out = {"v": 1, "request_id": request_id, "ok": False, "error": error,
           "reason": reason}
    out.update(extra)
    return out


def _ok(*, status: str, request_id: str, to: str, message_id: str | None,
        remaining_today: int, limit: int, resets_at: str) -> dict:
    return {
        "v": 1, "request_id": request_id, "ok": True,
        "status": status, "to": to,
        "message_id": message_id,
        "remaining_today": remaining_today,
        "limit": limit, "resets_at": resets_at,
    }


def handle_send(*, cfg: Config, caller: str, request: dict[str, Any]) -> dict:
    """Process one `op=send` request.

    Returns a JSON-serializable dict response. The daemon serializes it
    + newline + writes it back to the socket.
    """
    request_id = request.get("request_id", "")
    if not isinstance(request_id, str) or len(request_id) > 64:
        return _err(error="protocol", reason="malformed_request_id",
                    request_id="", detail="request_id must be string ≤64 chars")

    # ---- field validation -----------------------------------------------
    try:
        validated_to = validate_address_field(
            "to", request.get("to"), allow_display_name=False)
        to_addr = address_only(validated_to)
        subject = validate_subject(request.get("subject"))
        body = validate_body(request.get("body"))
        body_html = request.get("body_html")
        if body_html is not None and not isinstance(body_html, str):
            raise InvalidInput("invalid_field_type", "body_html")
        if isinstance(body_html, str) and body_html == "":
            body_html = None
        if body_html is not None and "\x00" in body_html:
            raise InvalidInput("null_byte_in_field", "body_html")
        raw_atts = request.get("attachments") or []
        if not isinstance(raw_atts, list):
            raise InvalidInput("invalid_field_type", "attachments")
    except InvalidInput as e:
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "deny", "outcome": e.reason,
            "to": _safe_to(request.get("to")),
        })
        return _err(error="invalid_input", reason=e.reason, detail=e.detail,
                    request_id=request_id)

    # ---- operator-config validation (from + reply-to) ------------------
    if not cfg.dry_run and not cfg.email_from:
        return _err(error="invalid_input", reason="email_from_missing",
                    request_id=request_id,
                    detail="set EMAIL_FROM in /etc/hermes-mailer/.env")
    try:
        from_field = validate_address_field(
            "from", cfg.email_from or "dryrun@hermes.local",
            allow_display_name=True)
        reply_to_field = (
            validate_address_field("reply_to", cfg.reply_to,
                                    allow_display_name=True)
            if cfg.reply_to else None
        )
    except InvalidInput as e:
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "internal_error", "outcome": "operator_config_invalid",
            "field": e.detail, "reason": e.reason,
        })
        return _err(error="invalid_input", reason=e.reason, detail=e.detail,
                    request_id=request_id)

    # ---- allowlist check (BEFORE attachment validation — Codex F1 still
    #      applies: don't be a file-existence oracle for non-allowlisted
    #      recipients) -----------------------------------------------------
    contacts, allowlist_err = allowlist.load_for_caller(
        caller=caller,
        single_path=cfg.allowlist_single_path,
        allowlists_dir=cfg.allowlists_dir,
    )
    if allowlist_err:
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "allowlist_load_warning",
            "outcome": "using_cache" if contacts else "no_cache",
            "detail": allowlist_err,
        })
    entry = allowlist.lookup(to_addr, contacts)
    if entry is None:
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "deny", "outcome": "not_in_allowlist", "to": to_addr,
        })
        return _err(error="not_allowed", reason="not_in_allowlist", to=to_addr,
                    request_id=request_id)
    limit = int(entry["daily_limit"])

    # ---- attachment validation -----------------------------------------
    # Decode base64 -> bytes here so the validator sees raw bytes. We do
    # NOT trust any size hint from the client; we measure ourselves.
    decoded_items = []
    for i, item in enumerate(raw_atts):
        if not isinstance(item, dict):
            audit.append(cfg.audit_log_path, {
                "request_id": request_id, "caller": caller,
                "event": "deny", "outcome": "attachment_invalid_item",
                "to": to_addr, "detail": f"index={i}",
            })
            return _err(error="invalid_input", reason="attachment_invalid_item",
                        detail=str(i), request_id=request_id)
        filename = item.get("filename")
        content_b64 = item.get("content_b64")
        if not isinstance(content_b64, str):
            audit.append(cfg.audit_log_path, {
                "request_id": request_id, "caller": caller,
                "event": "deny", "outcome": "attachment_missing_content",
                "to": to_addr,
            })
            return _err(error="invalid_input",
                        reason="attachment_missing_content",
                        detail=str(i), request_id=request_id)
        try:
            content = base64.b64decode(content_b64, validate=True)
        except Exception:
            audit.append(cfg.audit_log_path, {
                "request_id": request_id, "caller": caller,
                "event": "deny", "outcome": "attachment_base64_decode_failed",
                "to": to_addr,
            })
            return _err(error="invalid_input",
                        reason="attachment_base64_decode_failed",
                        detail=str(i), request_id=request_id)
        decoded_items.append({"filename": filename, "content": content})

    try:
        loaded = attachments.validate_all(
            decoded_items,
            max_attachment_bytes=cfg.max_attachment_bytes,
            max_total_bytes=cfg.max_total_bytes,
        )
    except InvalidInput as e:
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "deny", "outcome": e.reason,
            "to": to_addr, "detail": e.detail,
        })
        return _err(error="invalid_input", reason=e.reason, detail=e.detail,
                    request_id=request_id)

    # ---- reserve + send -------------------------------------------------
    local_day = ratelimit.local_day_str(cfg.limit_tz)
    resets_at = ratelimit.next_midnight_iso(cfg.limit_tz)
    subject_trunc = audit.trunc_subject(subject)
    byte_size = (sum(len(a.content) for a in loaded) + len(body.encode("utf-8"))
                 + len((body_html or "").encode("utf-8")))

    try:
        reserve_ctx = ratelimit.reserve(
            cfg.ratelimit_db_path,
            caller=caller,
            recipient=to_addr,
            limit=limit,
            local_day=local_day,
            subject_trunc=subject_trunc,
            byte_size=byte_size,
            attachment_count=len(loaded),
            request_id=request_id,
            ttl_seconds=cfg.reservation_ttl_seconds,
        )
        return _send_under_reservation(
            reserve_ctx=reserve_ctx, cfg=cfg, caller=caller, request_id=request_id,
            to_addr=to_addr, from_field=from_field, reply_to_field=reply_to_field,
            subject=subject, subject_trunc=subject_trunc, body=body,
            body_html=body_html, loaded=loaded, byte_size=byte_size,
            limit=limit, local_day=local_day, resets_at=resets_at,
        )
    except ratelimit.RateLimitExceeded as e:
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "deny", "outcome": "rate_limit_exceeded",
            "to": to_addr, "limit": e.limit, "sent_today": e.sent_today,
        })
        return _err(error="not_allowed", reason="rate_limit_exceeded",
                    to=to_addr, limit=e.limit, sent_today=e.sent_today,
                    resets_at=resets_at, request_id=request_id)


def _send_under_reservation(
    *, reserve_ctx, cfg: Config, caller: str, request_id: str,
    to_addr: str, from_field: str, reply_to_field: str | None,
    subject: str, subject_trunc: str, body: str, body_html: str | None,
    loaded: list, byte_size: int, limit: int, local_day: str, resets_at: str,
) -> dict:
    with reserve_ctx as reservation:
        rendered = RenderedEmail(
            to=to_addr, from_=from_field, reply_to=reply_to_field,
            subject=subject, text=body, html=body_html, attachments=loaded,
        )
        try:
            transport = make_transport(
                dry_run=cfg.dry_run, transport_name=cfg.transport, cfg=cfg,
            )
        except PreSendError as e:
            _finalize_or_warn(cfg, reservation, "failed_pre_send", caller, to_addr, request_id)
            audit.append(cfg.audit_log_path, {
                "request_id": request_id, "caller": caller,
                "event": "transport_failed", "outcome": "pre_send",
                "to": to_addr, "detail": str(e),
            })
            return _err(error="transport_failed", reason="pre_send", to=to_addr,
                        request_id=request_id)

        try:
            msg_id = transport.send(rendered)
        except PreSendError as e:
            _finalize_or_warn(cfg, reservation, "failed_pre_send", caller, to_addr, request_id)
            audit.append(cfg.audit_log_path, {
                "request_id": request_id, "caller": caller,
                "event": "transport_failed", "outcome": "pre_send",
                "to": to_addr, "transport": transport.name,
                "detail": str(e)[:200],
            })
            return _err(error="transport_failed", reason="pre_send", to=to_addr,
                        request_id=request_id)
        except Exception as e:
            _finalize_or_warn(cfg, reservation, "unknown_post_send", caller, to_addr, request_id)
            audit.append(cfg.audit_log_path, {
                "request_id": request_id, "caller": caller,
                "event": "transport_failed", "outcome": "post_send_unknown",
                "to": to_addr, "transport": transport.name,
                "exc_type": type(e).__name__, "detail": str(e)[:200],
            })
            return _err(error="transport_failed", reason="post_send_unknown",
                        to=to_addr, request_id=request_id)

        finalize_status = "dry_run" if (cfg.dry_run or transport.name == "dry_run") else "sent"
        _finalize_or_warn(cfg, reservation, finalize_status, caller, to_addr, request_id,
                          message_id=msg_id)
        sent_today = ratelimit.count_today(
            cfg.ratelimit_db_path,
            caller=caller, recipient=to_addr, local_day=local_day,
        )
        remaining = max(0, limit - sent_today)
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "send", "outcome": finalize_status,
            "to": to_addr, "subject": subject_trunc,
            "attachment_basenames": [a.name for a in loaded],
            "bytes": byte_size, "message_id": msg_id,
            "transport": transport.name,
            "remaining_today": remaining, "limit": limit,
        })
        return _ok(
            status=finalize_status, request_id=request_id, to=to_addr,
            message_id=msg_id, remaining_today=remaining,
            limit=limit, resets_at=resets_at,
        )


def _finalize_or_warn(cfg, reservation, new_status, caller, to_addr,
                      request_id, message_id=None):
    rowcount = ratelimit.finalize(cfg.ratelimit_db_path, reservation,
                                  new_status, message_id=message_id)
    if rowcount == 0:
        audit.append(cfg.audit_log_path, {
            "request_id": request_id, "caller": caller,
            "event": "reaper_collision", "outcome": new_status,
            "to": to_addr, "row_id": reservation.row_id,
            "message_id": message_id,
            "detail": "send completed but reservation was already reaped — "
                      "consider bumping EMAIL_RESERVATION_TTL_SECONDS",
        })


def _safe_to(value: Any) -> str:
    if not isinstance(value, str):
        return f"<non-string:{type(value).__name__}>"
    if len(value) > 320:
        return value[:320] + "…"
    return value.replace("\r", "\\r").replace("\n", "\\n")
