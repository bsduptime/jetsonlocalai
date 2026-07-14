"""greeninvoice plugin handlers — thin shims over the hermes-greeninvoice daemon.

All policy (rate limits, the issue confirmation gate, credential custody,
input validation, the GreenInvoice API calls) lives in the daemon. Each
handler here just forwards `args` to the matching daemon op over the Unix
socket and returns the response as a JSON string. Handlers NEVER raise.

Tool name -> daemon op is the tool name minus the `gi_` prefix.
"""

from __future__ import annotations

import json
import os
import stat

from . import _client

# Local file read limit for uploads (matches the daemon default). Env-tunable.
_MAX_UPLOAD_BYTES = int(os.environ.get("GI_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))

# Extension -> content type for the invoice files we accept.
_EXT_CONTENT_TYPE = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "webp": "image/webp", "heic": "image/heic",
    "heif": "image/heif", "gif": "image/gif",
}


def _allowed_upload_roots() -> list[str]:
    """Resolved directories a file may be uploaded from. Prefer an explicit
    GI_UPLOAD_ALLOWED_DIRS; otherwise fall back to Hermes' media allowlist
    (where received Telegram attachments land). Colon-separated."""
    raw = os.environ.get("GI_UPLOAD_ALLOWED_DIRS") or os.environ.get(
        "HERMES_MEDIA_ALLOW_DIRS") or ""
    roots = []
    for d in raw.split(os.pathsep):
        d = d.strip()
        if d:
            roots.append(os.path.realpath(d))
    return roots


def _read_upload_file(path) -> tuple[str, str, bytes]:
    """Validate `path` is a regular file inside the allowlist and read it.
    Race-safe: resolve, confine to an allowed root, then open with O_NOFOLLOW
    and re-check the opened fd. Returns (filename, content_type, data)."""
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    roots = _allowed_upload_roots()
    if not roots:
        raise PermissionError("no upload directories are allowlisted")
    real = os.path.realpath(path)
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise PermissionError("path is outside the allowed upload directories")
    filename = os.path.basename(real)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = _EXT_CONTENT_TYPE.get(ext)
    if content_type is None:
        raise ValueError(f"unsupported file type: .{ext}")
    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        # Re-validate the ACTUALLY-opened file, not just the path we resolved
        # before open(). Closes a parent-directory rename/symlink race between
        # realpath() and open() (O_NOFOLLOW only guards the final component):
        # resolve the fd back to a real path and require it still inside a root.
        try:
            opened = os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            opened = real
        if not any(opened == r or opened.startswith(r + os.sep) for r in roots):
            raise PermissionError("resolved file escaped the allowed directories")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError("not a regular file")
        if st.st_size <= 0 or st.st_size > _MAX_UPLOAD_BYTES:
            raise ValueError(f"file size {st.st_size} out of range (max {_MAX_UPLOAD_BYTES})")
        data = os.read(fd, st.st_size + 1)
    finally:
        os.close(fd)
    if len(data) != st.st_size:
        raise ValueError("file changed size during read")
    return filename, content_type, data


def _dispatch(op: str, args) -> str:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "args_not_object", "op": op,
        })
    try:
        resp = _client.call(op, args)
    except _client.DaemonUnreachable as e:
        return json.dumps({
            "ok": False, "error": "transport_failed",
            "reason": "daemon_unreachable", "op": op,
            "detail": f"{e.reason}: {e.detail}",
        })
    except Exception as e:  # last-resort net — must always return JSON
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "internal_error", "op": op,
            "detail": type(e).__name__,
        })
    # Drop the wire-protocol version key; the agent doesn't need it.
    if isinstance(resp, dict) and "v" in resp:
        resp = {k: v for k, v in resp.items() if k != "v"}
    return json.dumps(resp)


def gi_draft_invoice(args, **_kw):           return _dispatch("draft_invoice", args)
def gi_issue_invoice(args, **_kw):           return _dispatch("issue_invoice", args)
def gi_get_document(args, **_kw):            return _dispatch("get_document", args)
def gi_search_documents(args, **_kw):        return _dispatch("search_documents", args)
def gi_document_download_links(args, **_kw): return _dispatch("document_download_links", args)
def gi_create_client(args, **_kw):           return _dispatch("create_client", args)
def gi_update_client(args, **_kw):           return _dispatch("update_client", args)
def gi_get_client(args, **_kw):              return _dispatch("get_client", args)
def gi_search_clients(args, **_kw):          return _dispatch("search_clients", args)
def gi_quota(args, **_kw):                   return _dispatch("quota", args)

# ---- expenses -------------------------------------------------------------
def gi_create_expense(args, **_kw):          return _dispatch("create_expense", args)
def gi_get_expense(args, **_kw):             return _dispatch("get_expense", args)
def gi_search_expenses(args, **_kw):         return _dispatch("search_expenses", args)
def gi_delete_expense(args, **_kw):          return _dispatch("delete_expense", args)
def gi_close_expense(args, **_kw):           return _dispatch("close_expense", args)
def gi_search_expense_drafts(args, **_kw):   return _dispatch("search_expense_drafts", args)
def gi_create_supplier(args, **_kw):         return _dispatch("create_supplier", args)
def gi_search_suppliers(args, **_kw):        return _dispatch("search_suppliers", args)
def gi_get_classifications(args, **_kw):     return _dispatch("get_classifications", args)


def gi_upload_expense_file(args, **_kw) -> str:
    """Read the invoice file locally (allowlist-confined) and stream it to the
    daemon over the framed upload protocol. The file bytes never enter the
    LLM's args — only the `path` does."""
    if not isinstance(args, dict):
        return json.dumps({"ok": False, "error": "invalid_input",
                           "reason": "args_not_object", "op": "upload_expense_file"})
    try:
        filename, content_type, data = _read_upload_file(args.get("path"))
    except (ValueError, PermissionError, IsADirectoryError) as e:
        return json.dumps({"ok": False, "error": "invalid_input",
                           "reason": "file_rejected", "op": "upload_expense_file",
                           "detail": str(e)[:160]})
    except FileNotFoundError:
        return json.dumps({"ok": False, "error": "invalid_input",
                           "reason": "file_not_found", "op": "upload_expense_file"})
    except OSError as e:
        return json.dumps({"ok": False, "error": "invalid_input",
                           "reason": "file_unreadable", "op": "upload_expense_file",
                           "detail": type(e).__name__})

    # (An earlier image-classifier clearance check was removed when the safety gate moved
    # from the upload step to gi_create_expense — see hooks.py / confirmgate.py. Upload only
    # creates a reversible Morning OCR draft with no ledger numbers, so it doesn't need
    # gating; the numbers are confirmed at create-time instead.)
    try:
        resp = _client.call_with_file(
            "upload_expense_file",
            {"filename": filename, "content_type": content_type},
            data,
        )
    except _client.DaemonUnreachable as e:
        return json.dumps({"ok": False, "error": "transport_failed",
                           "reason": "daemon_unreachable", "op": "upload_expense_file",
                           "detail": f"{e.reason}: {e.detail}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": "invalid_input",
                           "reason": "internal_error", "op": "upload_expense_file",
                           "detail": type(e).__name__})
    if isinstance(resp, dict) and "v" in resp:
        resp = {k: v for k, v in resp.items() if k != "v"}
    return json.dumps(resp)


HANDLERS = {
    "gi_draft_invoice": gi_draft_invoice,
    "gi_issue_invoice": gi_issue_invoice,
    "gi_get_document": gi_get_document,
    "gi_search_documents": gi_search_documents,
    "gi_document_download_links": gi_document_download_links,
    "gi_create_client": gi_create_client,
    "gi_update_client": gi_update_client,
    "gi_get_client": gi_get_client,
    "gi_search_clients": gi_search_clients,
    "gi_quota": gi_quota,
    "gi_upload_expense_file": gi_upload_expense_file,
    "gi_create_expense": gi_create_expense,
    "gi_get_expense": gi_get_expense,
    "gi_search_expenses": gi_search_expenses,
    "gi_delete_expense": gi_delete_expense,
    "gi_close_expense": gi_close_expense,
    "gi_search_expense_drafts": gi_search_expense_drafts,
    "gi_create_supplier": gi_create_supplier,
    "gi_search_suppliers": gi_search_suppliers,
    "gi_get_classifications": gi_get_classifications,
}
