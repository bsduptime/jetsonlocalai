"""send_email handler.

Contract (Hermes plugin spec): handler ALWAYS returns a JSON string. Never
raises. Catches every exception class and maps to a documented response shape.
"""

from __future__ import annotations

import json
import traceback
from typing import Any

from . import allowlist, attachments, audit, ratelimit
from .config import Config, ensure_state_dirs, load_config
from .errors import (
    InvalidInput,
    PluginConfigError,
    PreSendError,
    invalid_input_response,
    not_allowed_response,
    ok_response,
    transport_failed_response,
)
from .headers import (
    address_only,
    validate_address_field,
    validate_body,
    validate_subject,
)
from .transport import RenderedEmail, make_transport


def send_email(args: dict, **_kwargs) -> str:  # noqa: C901 — one big switch is clearer
    try:
        cfg = load_config()
    except Exception as e:
        return json.dumps(invalid_input_response(
            reason="config_load_failed", detail=str(e)
        ))

    try:
        return _dispatch(args, cfg)
    except Exception as e:  # pragma: no cover — final safety net
        try:
            audit.append(cfg.audit_log_path, {
                "event": "internal_error",
                "outcome": "exception",
                "exc_type": type(e).__name__,
                "trace": traceback.format_exc(limit=5),
            })
        except Exception:
            pass
        return json.dumps(invalid_input_response(
            reason="internal_error", detail=type(e).__name__,
        ))


def _dispatch(args: dict, cfg: Config) -> str:
    ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)
    ratelimit.reap_stale_reservations(cfg.ratelimit_db_path, cfg.reservation_ttl_seconds)

    # ---- input validation -----------------------------------------------
    try:
        to_raw = args.get("to")
        validated_to = validate_address_field("to", to_raw, allow_display_name=False)
        to_addr = address_only(validated_to)
        subject = validate_subject(args.get("subject"))
        body = validate_body(args.get("body"))
        body_html = args.get("body_html") or None
        if body_html is not None and not isinstance(body_html, str):
            raise InvalidInput("invalid_field_type", "body_html")
        if body_html is not None and "\x00" in body_html:
            raise InvalidInput("null_byte_in_field", "body_html")
        raw_attachments = args.get("attachments") or []
        if not isinstance(raw_attachments, list):
            raise InvalidInput("invalid_field_type", "attachments")
    except InvalidInput as e:
        audit.append(cfg.audit_log_path, {
            "event": "deny",
            "outcome": e.reason,
            "to": _safe_to(args.get("to")),
        })
        return json.dumps(invalid_input_response(reason=e.reason, detail=e.detail))

    # `from` and `reply_to` come from operator-controlled env; still validate.
    if not cfg.dry_run and not cfg.email_from:
        return json.dumps(invalid_input_response(
            reason="email_from_missing",
            detail="set EMAIL_FROM in plugin .env",
        ))
    try:
        from_field = validate_address_field(
            "from", cfg.email_from or "dryrun@hermes.local",
            allow_display_name=True,
        )
        reply_to_field = (
            validate_address_field("reply_to", cfg.reply_to, allow_display_name=True)
            if cfg.reply_to else None
        )
    except InvalidInput as e:
        audit.append(cfg.audit_log_path, {
            "event": "internal_error",
            "outcome": "operator_config_invalid",
            "field": e.detail,
            "reason": e.reason,
        })
        return json.dumps(invalid_input_response(reason=e.reason, detail=e.detail))

    # ---- allowlist check ------------------------------------------------
    # Done BEFORE attachment validation so the tool can't be used as a
    # file-existence / file-type oracle for non-allowlisted recipients.
    contacts, allowlist_err = allowlist.load_allowlist(cfg.allowlist_path)
    if allowlist_err:
        audit.append(cfg.audit_log_path, {
            "event": "allowlist_load_warning",
            "outcome": "using_cache" if contacts else "no_cache",
            "detail": allowlist_err,
        })
    entry = allowlist.lookup(to_addr, contacts)
    if entry is None:
        audit.append(cfg.audit_log_path, {
            "event": "deny",
            "outcome": "not_in_allowlist",
            "to": to_addr,
        })
        return json.dumps(not_allowed_response(reason="not_in_allowlist", to=to_addr))

    limit = int(entry["daily_limit"])

    # ---- attachment validation ------------------------------------------
    try:
        loaded = attachments.load_all(
            raw_attachments,
            max_attachment_bytes=cfg.max_attachment_bytes,
            max_total_bytes=cfg.max_total_bytes,
            allowed_prefixes=cfg.allowed_prefixes,
        )
    except InvalidInput as e:
        audit.append(cfg.audit_log_path, {
            "event": "deny",
            "outcome": e.reason,
            "to": to_addr,
            "detail": e.detail,
        })
        return json.dumps(invalid_input_response(reason=e.reason, detail=e.detail))

    # ---- reserve slot + send --------------------------------------------
    local_day = ratelimit.local_day_str(cfg.limit_tz)
    resets_at = ratelimit.next_midnight_iso(cfg.limit_tz)
    subject_trunc = audit.trunc_subject(subject)
    byte_size = sum(len(a.content) for a in loaded) + len(body.encode("utf-8")) + len(
        (body_html or "").encode("utf-8")
    )

    try:
        reserve_ctx = ratelimit.reserve(
            cfg.ratelimit_db_path,
            recipient=to_addr,
            limit=limit,
            local_day=local_day,
            subject_trunc=subject_trunc,
            byte_size=byte_size,
            attachment_count=len(loaded),
            ttl_seconds=cfg.reservation_ttl_seconds,
        )
        # The RateLimitExceeded actually surfaces from __enter__, NOT from
        # the generator call above (because @contextmanager defers body
        # execution). So we need the try/except to span the `with` itself.
        return _send_under_reservation(
            reserve_ctx=reserve_ctx, cfg=cfg, to_addr=to_addr,
            from_field=from_field, reply_to_field=reply_to_field,
            subject=subject, subject_trunc=subject_trunc,
            body=body, body_html=body_html, loaded=loaded,
            byte_size=byte_size,
            limit=limit, local_day=local_day, resets_at=resets_at,
        )
    except ratelimit.RateLimitExceeded as e:
        audit.append(cfg.audit_log_path, {
            "event": "deny",
            "outcome": "rate_limit_exceeded",
            "to": to_addr,
            "limit": e.limit,
            "sent_today": e.sent_today,
        })
        return json.dumps(not_allowed_response(
            reason="rate_limit_exceeded",
            to=to_addr,
            limit=e.limit,
            sent_today=e.sent_today,
            resets_at=resets_at,
        ))


def _send_under_reservation(
    *, reserve_ctx, cfg, to_addr, from_field, reply_to_field,
    subject, subject_trunc, body, body_html, loaded, byte_size,
    limit, local_day, resets_at,
) -> str:
    with reserve_ctx as reservation:
        # Build the rendered email and pick transport.
        rendered = RenderedEmail(
            to=to_addr,
            from_=from_field,
            reply_to=reply_to_field,
            subject=subject,
            text=body,
            html=body_html,
            attachments=loaded,
        )
        try:
            transport = make_transport(
                dry_run=cfg.dry_run,
                transport_name=cfg.transport,
                cfg=cfg,
            )
        except PreSendError as e:
            _finalize_or_warn(cfg, reservation, "failed_pre_send", to_addr)
            audit.append(cfg.audit_log_path, {
                "event": "transport_failed",
                "outcome": "pre_send",
                "to": to_addr,
                "detail": str(e),
            })
            return json.dumps(transport_failed_response(reason="pre_send", to=to_addr))

        try:
            msg_id = transport.send(rendered)
        except PreSendError as e:
            _finalize_or_warn(cfg, reservation, "failed_pre_send", to_addr)
            audit.append(cfg.audit_log_path, {
                "event": "transport_failed",
                "outcome": "pre_send",
                "to": to_addr,
                "transport": transport.name,
                "detail": str(e)[:200],
            })
            return json.dumps(transport_failed_response(reason="pre_send", to=to_addr))
        except Exception as e:
            _finalize_or_warn(cfg, reservation, "unknown_post_send", to_addr)
            audit.append(cfg.audit_log_path, {
                "event": "transport_failed",
                "outcome": "post_send_unknown",
                "to": to_addr,
                "transport": transport.name,
                "exc_type": type(e).__name__,
                "detail": str(e)[:200],
            })
            return json.dumps(transport_failed_response(
                reason="post_send_unknown", to=to_addr,
            ))

        finalize_status = "dry_run" if (cfg.dry_run or transport.name == "dry_run") else "sent"
        _finalize_or_warn(cfg, reservation, finalize_status, to_addr, message_id=msg_id)
        sent_today_after = ratelimit.count_today(cfg.ratelimit_db_path, to_addr, local_day)
        remaining = max(0, limit - sent_today_after)

        audit.append(cfg.audit_log_path, {
            "event": "send",
            "outcome": finalize_status,
            "to": to_addr,
            "subject": subject_trunc,
            "attachment_basenames": [a.name for a in loaded],
            "bytes": byte_size,
            "message_id": msg_id,
            "transport": transport.name,
            "remaining_today": remaining,
            "limit": limit,
        })
        return json.dumps(ok_response(
            status=finalize_status,
            to=to_addr,
            message_id=msg_id,
            remaining_today=remaining,
            limit=limit,
            resets_at=resets_at,
        ))


def _safe_to(value: Any) -> str:
    """Render the agent's `to` value for the audit log without trusting it."""
    if not isinstance(value, str):
        return f"<non-string:{type(value).__name__}>"
    if len(value) > 320:
        return value[:320] + "…"
    return value.replace("\r", "\\r").replace("\n", "\\n")


def _finalize_or_warn(cfg, reservation, new_status: str, to_addr: str,
                      message_id: str | None = None) -> None:
    """Finalize a reservation; emit a `reaper_collision` audit line if the
    row was already reaped by the stale-reservation cleanup. The quota
    accounting is still correct (the reaped row counts) — but the audit
    trail loses the message_id and final status linkage."""
    rowcount = ratelimit.finalize(cfg.ratelimit_db_path, reservation,
                                  new_status, message_id=message_id)
    if rowcount == 0:
        audit.append(cfg.audit_log_path, {
            "event": "reaper_collision",
            "outcome": new_status,
            "to": to_addr,
            "row_id": reservation.row_id,
            "message_id": message_id,
            "detail": "send completed but reservation was already reaped — "
                      "consider bumping EMAIL_RESERVATION_TTL_SECONDS",
        })
