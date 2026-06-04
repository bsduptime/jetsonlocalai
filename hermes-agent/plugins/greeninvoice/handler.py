"""greeninvoice plugin handlers — thin shims over the hermes-greeninvoice daemon.

All policy (rate limits, the issue confirmation gate, credential custody,
input validation, the GreenInvoice API calls) lives in the daemon. Each
handler here just forwards `args` to the matching daemon op over the Unix
socket and returns the response as a JSON string. Handlers NEVER raise.

Tool name -> daemon op is the tool name minus the `gi_` prefix.
"""

from __future__ import annotations

import json

from . import _client


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
}
