"""visiongate hooks — the two Hermes extension points the guardrail hangs off.

`pre_gateway_dispatch`  advisory: classify inbound media before the LLM turn, append a
                        sanitised annotation. Never blocks.
`pre_tool_call`         enforcing: gate gi_upload_expense_file. A file that doesn't look
                        like a tax document escalates to the human approval prompt in
                        Telegram, which the LLM cannot bypass.

Neither hook may raise. `model_tools.py` catches exceptions out of the pre_tool_call
resolution path and PROCEEDS with the call (fail-open), so an exception here would
silently disable the gate — the opposite of what it is for. Every entry point is
wrapped.
"""

from __future__ import annotations

import logging
import os

from . import visiongate as vg
from .handler import _read_upload_file

log = logging.getLogger("hermes.plugins.greeninvoice.hooks")

GATED_TOOLS = {"gi_upload_expense_file"}


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def pre_gateway_dispatch(event=None, **_kw):
    """Classify inbound images/PDFs and tell Elena what she is looking at.

    Runs before auth and before the LLM. The media is already on disk by now
    (the Telegram adapter downloads it, and event.media_urls holds local paths).

    CRITICAL: this executes on the gateway's asyncio EVENT LOOP (invoke_hook is
    synchronous, called from `async def _handle_message`). Blocking here stalls the whole
    gateway, so every classification is offloaded to a thread with a bounded wait. This
    hook is advisory ONLY — if it gives up, nothing is lost but the annotation, and the
    real gate (pre_tool_call, on a worker thread) still classifies before any upload.
    """
    if not vg.ENABLED or event is None:
        return None
    try:
        paths = [p for p in (getattr(event, "media_urls", None) or [])
                 if isinstance(p, str)]
        if not paths:
            return None

        verdicts = []
        for path in paths:
            if _ext(path) not in vg.IMAGE_EXT | vg.PDF_EXT:
                continue
            v = vg.lookup_by_path(path)   # stat() only — no read, no hash, no network
            if v:
                verdicts.append(v)
            else:
                vg.warm(path)             # classify in a worker; warms the upload gate

        if not verdicts:
            return None

        text = event.text or ""
        # The rewrite lands BEFORE slash-command parsing, and command recognition
        # requires the text to start with "/" and parses the remainder as args
        # (gateway/platforms/base.py:1813-1829). Appending to "/approve" would corrupt
        # its arguments, so we leave command messages strictly alone. The verdicts are
        # still cached, so the upload gate works regardless.
        if text.lstrip().startswith("/"):
            return None

        annotated = (text + "\n\n" + vg.annotate(verdicts)).strip()
        return {"action": "rewrite", "text": annotated}
    except Exception:
        log.exception("visiongate: pre_gateway_dispatch failed (message passes through)")
        return None


def pre_tool_call(tool_name: str = "", args=None, **_kw):
    """Gate the expense upload.

    Returns an `approve` directive — NOT `block` — when the file does not look like a
    tax document, or when we could not classify it at all. Rationale: a false reject
    (refusing a real receipt) costs David a real expense and his trust, while a false
    accept costs one junk OCR draft he deletes by hand. So we bias to accept and ask.

    The human prompt IS the override. There is deliberately no `force` argument on the
    tool, because anything the model can set is reachable by a prompt injection, which
    is precisely what this gate exists to stop.
    """
    if not vg.ENABLED or tool_name not in GATED_TOOLS:
        return None
    try:
        path = (args or {}).get("path")
        if not isinstance(path, str) or not path:
            return None  # the handler will reject it with its own error

        # Reuse the handler's fd-safe, allowlist-confined reader so the gate and the
        # upload agree on exactly which file they are talking about.
        try:
            filename, _ctype, data = _read_upload_file(path)
        except Exception:
            return None  # unreadable/disallowed — let the handler produce the error

        digest = vg.sha256(data)
        verdict = vg.cached(digest) or vg.classify(data, _ext(filename))

        # Clear this exact content either way: the handler requires a clearance for the
        # bytes it ships, and on an `approve` directive the tool only ever runs if the
        # human says yes.
        vg.clear_for_upload(digest)

        vg.audit("gate tool=%s file=%s verdict=%s sha=%s",
                 tool_name, os.path.basename(path),
                 verdict["kind"] if verdict else "UNKNOWN", digest[:12])

        if verdict and verdict["is_tax_document"] and not vg.OBSERVE:
            return None  # looks like a real invoice/receipt — proceed silently

        if vg.OBSERVE:
            # Observation mode: even a perfect receipt stops here and asks. Used while
            # benchmarking against real receipts, so nothing can reach the live Morning
            # account (GI_DRY_RUN=false — these are REAL writes) by accident.
            return {"action": "approve",
                    "message": (f"[visiongate OBSERVE mode] Local check says: "
                                f"{verdict['kind'] if verdict else 'UNKNOWN'}"
                                + (f" (confidence {verdict['confidence']:.2f})"
                                   if verdict else "")
                                + ". Upload to Morning for real?"),
                    "rule_key": "visiongate:observe"}

        if verdict:
            # Enum + number only. The approval message is shown to David but ALSO comes
            # back to the model as the tool result on a denial, so it is exactly as
            # untrusted a channel as the annotation — no model-authored prose here either.
            why = (f"This does not look like an invoice or receipt. The local image check "
                   f"says: {verdict['kind']} (confidence {verdict['confidence']:.2f}, "
                   f"language {verdict['language']}). "
                   f"Upload it to Morning as a business expense anyway?")
        else:
            why = ("The local image check could not classify this file (model "
                   "unavailable, timed out, or unsupported type). Upload it to Morning "
                   "as a business expense anyway?")
        return {"action": "approve", "message": why,
                "rule_key": "visiongate:non_invoice"}
    except Exception:
        # Never raise: an exception here is swallowed upstream and the call PROCEEDS.
        # Ask the human instead of silently failing open.
        log.exception("visiongate: pre_tool_call failed; escalating to human approval")
        return {"action": "approve",
                "message": ("The local image check errored. Upload this file to Morning "
                            "as a business expense anyway?"),
                "rule_key": "visiongate:error"}
