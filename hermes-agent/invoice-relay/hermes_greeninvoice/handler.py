"""Request handler — the policy enforcement core.

Called by the daemon for each incoming request. Returns a JSON-serializable
dict. NEVER raises; all errors become response dicts (see PROTOCOL.md).

Trust model: every gated, side-effecting op (issue a document, write a
client) passes through the rate limiter in a process the caller cannot
bypass. The caller is identified by SO_PEERCRED, not by anything in the
request, so it cannot spoof a different identity to get a fresh quota.
"""

from __future__ import annotations

from typing import Any, Callable

from . import audit, previews, ratelimit, validate
from .config import Config
from .errors import InvalidInput, NotAllowed, UpstreamError

# ---- op registry ---------------------------------------------------------
# Each op: (action_class | None, read_only).
# action_class None  -> unlimited (reads + quota).
# read_only True     -> never touches upstream in a mutating way; in dry-run
#                       these still return synthetic data.
_OPS = {
    "draft_invoice":            ("draft", False),
    "issue_invoice":            ("issue", False),
    "get_document":             (None, True),
    "search_documents":         (None, True),
    "document_download_links":  (None, True),
    "create_client":            ("client_write", False),
    "update_client":            ("client_write", False),
    "get_client":               (None, True),
    "search_clients":           (None, True),
    "quota":                    (None, True),
    # ---- expenses (vendor-side ledger) ----
    "create_expense":           ("expense_write", False),
    "get_expense":              (None, True),
    "search_expenses":          (None, True),
    "delete_expense":           ("expense_write", False),
    # close_expense reports the expense to tax (status 10 -> 20, irreversible).
    # It shares the tight `issue` class and is confirm-gated (see below).
    "close_expense":            ("issue", False),
    "upload_expense_file":      ("expense_upload", False),
    "search_expense_drafts":    (None, True),
    "create_supplier":          ("expense_write", False),
    "search_suppliers":         (None, True),
    "get_classifications":      (None, True),
}

# Ops that create a REAL, irreversible tax record: require an explicit
# literal-`true` confirm flag (defense in depth on top of the rate limit).
CONFIRM_REQUIRED_OPS = {"issue_invoice", "close_expense"}

_DOC_SEARCH_FIELDS = {"type", "status", "fromDate", "toDate", "clientId", "text"}
_CLIENT_SEARCH_FIELDS = {"name", "taxId", "email", "text", "active"}
_EXPENSE_SEARCH_FIELDS = {
    "supplierId", "supplierName", "number", "fromDate", "toDate",
    "minAmount", "maxAmount", "description", "accountingClassificationId",
    "paid", "reported",
}
_EXPENSE_SEARCH_BOOL = {"paid", "reported"}
_EXPENSE_DRAFT_SEARCH_FIELDS = {
    "supplierId", "supplierName", "fromDate", "toDate", "description",
}
_SUPPLIER_SEARCH_FIELDS = {"name", "email", "contactPerson", "active"}
_SUPPLIER_SEARCH_BOOL = {"active"}


def _err(*, error: str, reason: str, request_id: str, op: str = "", **extra) -> dict:
    out = {"v": 1, "request_id": request_id, "ok": False, "error": error,
           "reason": reason}
    if op:
        out["op"] = op
    out.update(extra)
    return out


def _rate_snapshot(cfg: Config, caller: str, action_class: str) -> dict:
    per_hour, per_day = cfg.limits[action_class]
    local_day = ratelimit.local_day_str(cfg.limit_tz)
    try:
        used_hour, used_day = ratelimit.usage(
            cfg.ratelimit_db_path, caller=caller,
            action_class=action_class, local_day=local_day,
        )
    except Exception:
        used_hour = used_day = None
    return {
        "action_class": action_class,
        "per_hour": per_hour,
        "per_day": per_day,
        "used_hour": used_hour,
        "used_day": used_day,
        "remaining_hour": (None if used_hour is None else max(0, per_hour - used_hour)),
        "remaining_day": (None if used_day is None else max(0, per_day - used_day)),
        "resets_at": ratelimit.next_midnight_iso(cfg.limit_tz),
    }


def handle(*, cfg: Config, caller: str, request: dict[str, Any],
           get_client: Callable[[], Any], file_body: bytes | None = None) -> dict:
    """Dispatch one request. `get_client` lazily returns a live
    GreenInvoiceClient (only called in live mode for ops that hit upstream).
    `file_body` carries the raw bytes of a framed upload_expense_file request
    (None for every other op)."""
    request_id = request.get("request_id", "")
    if not isinstance(request_id, str) or len(request_id) > 64:
        return _err(error="protocol", reason="malformed_request_id", request_id="")

    op = request.get("op")
    if op not in _OPS:
        return _err(error="protocol", reason="unknown_op",
                    request_id=request_id, detail=str(op)[:40])
    action_class, read_only = _OPS[op]
    args = request.get("args") if isinstance(request.get("args"), dict) else {}

    try:
        if op == "quota":
            return _handle_quota(cfg, caller, request_id)

        # Build + validate the upstream call (method, path, body) for this op.
        plan = _plan_op(op, args, cfg)

        # ---- gated ops: confirmation + rate limit ----------------------
        if action_class is not None:
            # Strict identity check: only a real JSON `true` confirms. A
            # truthy string like "false"/"no" or an int must NOT pass.
            if op in CONFIRM_REQUIRED_OPS and args.get("confirm") is not True:
                audit.append(cfg.audit_log_path, {
                    "caller": caller, "op": op, "outcome": "deny",
                    "reason": "confirmation_required", "request_id": request_id,
                })
                return _err(error="not_allowed", reason="confirmation_required",
                            request_id=request_id, op=op,
                            detail="set args.confirm=true for this irreversible action")
            return _run_gated(cfg, caller, op, action_class, plan,
                              request_id, get_client, args, file_body)

        # ---- ungated reads --------------------------------------------
        return _run_read(cfg, caller, op, plan, request_id, get_client)

    except InvalidInput as e:
        audit.append(cfg.audit_log_path, {
            "caller": caller, "op": op, "outcome": "deny",
            "reason": e.reason, "detail": e.detail, "request_id": request_id,
        })
        return _err(error="invalid_input", reason=e.reason, detail=e.detail,
                    request_id=request_id, op=op)
    except Exception as e:  # last-resort net; handler must not raise
        audit.append(cfg.audit_log_path, {
            "caller": caller, "op": op, "outcome": "internal_error",
            "detail": type(e).__name__, "request_id": request_id,
        })
        return _err(error="invalid_input", reason="internal_error",
                    detail=type(e).__name__, request_id=request_id, op=op)


# ---- per-op plans --------------------------------------------------------

class _Plan:
    __slots__ = ("method", "path", "body", "params", "synthetic", "idempotent")

    def __init__(self, method, path, *, body=None, params=None, synthetic=None,
                 idempotent=True):
        self.method = method
        self.path = path
        self.body = body
        self.params = params
        self.synthetic = synthetic  # dict returned in dry-run instead of calling
        # idempotent=False ops must never be auto-retried on an ambiguous
        # 5xx: re-POSTing /documents could create a SECOND irreversible
        # document. Only 429 (provably not processed) may be retried.
        self.idempotent = idempotent


def _doc_id(args: dict) -> str:
    did = args.get("id")
    if not isinstance(did, str) or not did or len(did) > 64 or "/" in did:
        raise InvalidInput("invalid_document_id", "id")
    return did


def _client_id(args: dict) -> str:
    cid = args.get("id")
    if not isinstance(cid, str) or not cid or len(cid) > 64 or "/" in cid:
        raise InvalidInput("invalid_client_id", "id")
    return cid


def _expense_id(args: dict) -> str:
    eid = args.get("id")
    if not isinstance(eid, str) or not eid or len(eid) > 64 or "/" in eid:
        raise InvalidInput("invalid_expense_id", "id")
    return eid


def _plan_op(op: str, args: dict, cfg: Config) -> _Plan:
    if op == "draft_invoice":
        body = validate.build_document(args, for_issue=False)
        return _Plan("POST", "/documents/preview", body=body,
                     synthetic={"preview": True, "type": body["type"]})
    if op == "issue_invoice":
        body = validate.build_document(args, for_issue=True)
        return _Plan("POST", "/documents", body=body, idempotent=False,
                     synthetic={"id": "dryrun-doc", "type": body["type"],
                                "number": "DRYRUN", "emailed": bool(body["client"].get("emails"))})
    if op == "get_document":
        did = _doc_id(args)
        return _Plan("GET", f"/documents/{did}", synthetic={"id": did})
    if op == "search_documents":
        body = validate.build_search(args, allowed_fields=_DOC_SEARCH_FIELDS)
        return _Plan("POST", "/documents/search", body=body,
                     synthetic={"items": [], "total": 0})
    if op == "document_download_links":
        did = _doc_id(args)
        return _Plan("GET", f"/documents/{did}/download/links",
                     synthetic={"id": did, "links": []})
    if op == "create_client":
        body = validate.build_client_create(args)
        return _Plan("POST", "/clients", body=body, idempotent=False,
                     synthetic={"id": "dryrun-client", "name": body["name"]})
    if op == "update_client":
        cid, body = validate.build_client_update(args)
        return _Plan("PUT", f"/clients/{cid}", body=body,
                     synthetic={"id": cid, "name": body["name"]})
    if op == "get_client":
        cid = _client_id(args)
        return _Plan("GET", f"/clients/{cid}", synthetic={"id": cid})
    if op == "search_clients":
        body = validate.build_search(args, allowed_fields=_CLIENT_SEARCH_FIELDS)
        return _Plan("POST", "/clients/search", body=body,
                     synthetic={"items": [], "total": 0})

    # ---- expenses --------------------------------------------------------
    if op == "create_expense":
        body = validate.build_expense(args)
        return _Plan("POST", "/expenses", body=body, idempotent=False,
                     synthetic={"id": "dryrun-expense", "status": 10,
                                "documentType": body["documentType"]})
    if op == "get_expense":
        eid = _expense_id(args)
        return _Plan("GET", f"/expenses/{eid}", synthetic={"id": eid, "status": 10})
    if op == "search_expenses":
        body = validate.build_search(args, allowed_fields=_EXPENSE_SEARCH_FIELDS,
                                     bool_fields=_EXPENSE_SEARCH_BOOL)
        return _Plan("POST", "/expenses/search", body=body,
                     synthetic={"items": [], "total": 0})
    if op == "delete_expense":
        eid = _expense_id(args)
        return _Plan("DELETE", f"/expenses/{eid}", idempotent=False,
                     synthetic={"id": eid, "deleted": True})
    if op == "close_expense":
        eid = _expense_id(args)
        return _Plan("POST", f"/expenses/{eid}/close", idempotent=False,
                     synthetic={"id": eid, "status": 20})
    if op == "upload_expense_file":
        meta = validate.build_upload_meta(args, max_file_bytes=cfg.max_upload_file_bytes)
        # Special method: a two-call flow (get presigned URL -> POST file to
        # S3). `body` carries the validated upload metadata.
        return _Plan("UPLOAD", "/file-upload/v1/url", body=meta, idempotent=False,
                     synthetic={"uploaded": True, "dry_run": True,
                                "filename": meta["filename"]})
    if op == "search_expense_drafts":
        body = validate.build_search(args, allowed_fields=_EXPENSE_DRAFT_SEARCH_FIELDS)
        return _Plan("POST", "/expenses/drafts/search", body=body,
                     synthetic={"items": [], "total": 0})
    if op == "create_supplier":
        body = validate.build_supplier_create(args)
        return _Plan("POST", "/suppliers", body=body, idempotent=False,
                     synthetic={"id": "dryrun-supplier", "name": body["name"]})
    if op == "search_suppliers":
        body = validate.build_search(args, allowed_fields=_SUPPLIER_SEARCH_FIELDS,
                                     bool_fields=_SUPPLIER_SEARCH_BOOL)
        return _Plan("POST", "/suppliers/search", body=body,
                     synthetic={"items": [], "total": 0})
    if op == "get_classifications":
        return _Plan("GET", "/accounting/classifications/map",
                     synthetic={"items": []})
    raise InvalidInput("unknown_op", op)  # unreachable (guarded earlier)


# ---- execution -----------------------------------------------------------

def _call_upstream(plan: _Plan, get_client, file_body: bytes | None = None) -> Any:
    client = get_client()
    if plan.method == "UPLOAD":
        return _do_upload(plan, client, file_body)
    if plan.method == "GET":
        return client.get(plan.path, params=plan.params)
    if plan.method == "PUT":
        return client.put(plan.path, plan.body or {}, idempotent=plan.idempotent)
    if plan.method == "DELETE":
        return client.request("DELETE", plan.path, idempotent=plan.idempotent)
    return client.post(plan.path, plan.body or {}, idempotent=plan.idempotent)


def _do_upload(plan: _Plan, client, file_body: bytes | None) -> Any:
    """Two-call expense file upload: fetch a presigned S3 POST, then POST the
    file. The raw bytes are the framed body of the request (never in the JSON).
    The resulting OCR draft is created asynchronously upstream."""
    meta = plan.body or {}
    if not isinstance(file_body, (bytes, bytearray)):
        raise UpstreamError("upload_missing_body", detail="no file bytes")
    if len(file_body) != meta.get("byte_len"):
        raise UpstreamError(
            "upload_length_mismatch",
            detail=f"{len(file_body)}!={meta.get('byte_len')}")
    resp = client.get_upload_url()
    if not isinstance(resp, dict) or not resp.get("url") or not isinstance(resp.get("fields"), dict):
        raise UpstreamError("api_error", detail="malformed upload-url response")
    client.upload_file_to_s3(
        resp["url"], resp["fields"], filename=meta["filename"],
        content_type=meta["content_type"], data=bytes(file_body))
    return {
        "uploaded": True,
        "filename": meta["filename"],
        "bytes": len(file_body),
        "note": ("OCR draft is created asynchronously; find it via "
                 "search_expense_drafts, then create the expense with create_expense"),
    }


def _run_read(cfg, caller, op, plan, request_id, get_client) -> dict:
    if cfg.dry_run:
        result = plan.synthetic
        return _ok(request_id, op, result, dry_run=True)
    try:
        result = _call_upstream(plan, get_client)
    except UpstreamError as e:
        audit.append(cfg.audit_log_path, {
            "caller": caller, "op": op, "outcome": "upstream_failed",
            "reason": e.reason, "status": e.status, "request_id": request_id,
        })
        return _err(error="upstream_failed", reason=e.reason,
                    detail=e.detail, status=e.status,
                    request_id=request_id, op=op)
    audit.append(cfg.audit_log_path, {
        "caller": caller, "op": op, "outcome": "ok", "request_id": request_id,
    })
    return _ok(request_id, op, result, dry_run=False)


def _run_gated(cfg, caller, op, action_class, plan, request_id, get_client, args,
               file_body: bytes | None = None) -> dict:
    per_hour, per_day = cfg.limits[action_class]
    local_day = ratelimit.local_day_str(cfg.limit_tz)
    detail = _audit_detail(op, plan, args)

    # NOTE: ratelimit.reserve is a context manager — RateLimitExceeded is
    # raised on __enter__ (at the `with`), not when reserve() is called. So
    # the `with` MUST sit inside this try/except, not after it.
    try:
        with ratelimit.reserve(
            cfg.ratelimit_db_path, caller=caller, action_class=action_class,
            op=op, per_hour=per_hour, per_day=per_day, local_day=local_day,
            request_id=request_id, detail=detail,
            ttl_seconds=cfg.reservation_ttl_seconds,
        ) as reservation:
            return _execute_reserved(
                cfg=cfg, caller=caller, op=op, action_class=action_class,
                plan=plan, request_id=request_id, get_client=get_client,
                detail=detail, reservation=reservation, file_body=file_body)
    except ratelimit.RateLimitExceeded as e:
        audit.append(cfg.audit_log_path, {
            "caller": caller, "op": op, "outcome": "deny",
            "reason": "rate_limit_exceeded", "window": e.window,
            "limit": e.limit, "used": e.used, "request_id": request_id,
        })
        return _err(error="not_allowed", reason="rate_limit_exceeded",
                    request_id=request_id, op=op, window=e.window,
                    limit=e.limit, used=e.used,
                    rate=_rate_snapshot(cfg, caller, action_class))


def _execute_reserved(*, cfg, caller, op, action_class, plan, request_id,
                      get_client, detail, reservation, file_body=None) -> dict:
    """Run the side-effecting call under an already-acquired reservation,
    then finalize. Returns the response dict."""
    if cfg.dry_run:
        ratelimit.finalize(cfg.ratelimit_db_path, reservation, "committed",
                           detail="dry_run")
        audit.append(cfg.audit_log_path, {
            "caller": caller, "op": op, "outcome": "ok_dry_run",
            "detail": detail, "request_id": request_id,
        })
        return _ok(request_id, op, plan.synthetic, dry_run=True,
                   rate=_rate_snapshot(cfg, caller, action_class))

    try:
        result = _call_upstream(plan, get_client, file_body)
    except UpstreamError as e:
        # Distinguish "definitely no side effect" (clean 4xx) from
        # "ambiguous" (network error / 5xx after retries). The former
        # frees the quota slot; the latter conservatively keeps it.
        ambiguous = e.status is None or (e.status is not None and e.status >= 500)
        ratelimit.finalize(
            cfg.ratelimit_db_path, reservation,
            "unknown" if ambiguous else "failed_pre_send",
            detail=f"{e.reason}:{e.status}",
        )
        audit.append(cfg.audit_log_path, {
            "caller": caller, "op": op, "outcome": "upstream_failed",
            "reason": e.reason, "status": e.status,
            "quota_kept": ambiguous, "request_id": request_id,
        })
        return _err(error="upstream_failed", reason=e.reason,
                    detail=e.detail, status=e.status,
                    request_id=request_id, op=op,
                    rate=_rate_snapshot(cfg, caller, action_class))

    ratelimit.finalize(cfg.ratelimit_db_path, reservation, "committed")
    # Spool any rendered preview PDF to a file so the base64 blob doesn't
    # ride back over the socket into the agent's context.
    result = previews.spool(cfg, result, request_id)
    audit.append(cfg.audit_log_path, {
        "caller": caller, "op": op, "outcome": "ok",
        "detail": detail, "request_id": request_id,
        "pdf": (result.get("preview_pdf_path") if isinstance(result, dict) else None),
    })
    return _ok(request_id, op, result, dry_run=False,
               rate=_rate_snapshot(cfg, caller, action_class))


def _handle_quota(cfg, caller, request_id) -> dict:
    snapshot = {
        klass: _rate_snapshot(cfg, caller, klass)
        for klass in cfg.limits
    }
    return {
        "v": 1, "request_id": request_id, "ok": True, "op": "quota",
        "dry_run": cfg.dry_run, "env": cfg.env, "quotas": snapshot,
    }


def _audit_detail(op: str, plan: _Plan, args: dict) -> str:
    if op in ("draft_invoice", "issue_invoice"):
        body = plan.body or {}
        client = body.get("client", {})
        who = client.get("id") or audit.trunc(client.get("name"), 40)
        n = len(body.get("income", []))
        emailed = bool(client.get("emails"))
        return f"type={body.get('type')} client={who} rows={n} email={emailed}"
    if op in ("create_client", "update_client"):
        return f"name={audit.trunc((plan.body or {}).get('name'), 40)}"
    if op == "create_expense":
        body = plan.body or {}
        sup = body.get("supplier", {})
        who = sup.get("id") or audit.trunc(sup.get("name"), 40)
        return (f"docType={body.get('documentType')} supplier={who} "
                f"amount={body.get('amount')} num={audit.trunc(body.get('number'), 30)}")
    if op in ("get_expense", "delete_expense", "close_expense"):
        return f"id={audit.trunc(args.get('id'), 40)}"
    if op == "create_supplier":
        return f"name={audit.trunc((plan.body or {}).get('name'), 40)}"
    if op == "upload_expense_file":
        # Filename only — NEVER the file bytes.
        return f"file={audit.trunc((plan.body or {}).get('filename'), 60)}"
    return ""


def _ok(request_id: str, op: str, result: Any, *, dry_run: bool,
        rate: dict | None = None) -> dict:
    out = {"v": 1, "request_id": request_id, "ok": True, "op": op,
           "dry_run": dry_run, "result": result}
    if rate is not None:
        out["rate"] = rate
    return out
