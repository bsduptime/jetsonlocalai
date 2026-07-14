"""visiongate hooks — the two Hermes extension points the guardrail hangs off.

`pre_tool_call`         ENFORCING, and the only one that matters. Gates
                        gi_upload_expense_file: a file that doesn't look like a tax
                        document escalates to Hermes' human approval prompt in Telegram,
                        which the agent cannot bypass.
`pre_gateway_dispatch`  OBSERVING ONLY. Pre-warms the classifier so the gate is fast.
                        Never rewrites the message (it used to append a classification
                        for Elena; see the note on that function for why that's gone).

Neither hook may raise. `model_tools.py` catches exceptions out of the pre_tool_call
resolution path and PROCEEDS with the call (fail-open), so an exception here would
silently disable the gate — the exact opposite of what it exists for. Every entry point
is wrapped, and the failure path escalates to a human rather than staying quiet.
"""

from __future__ import annotations

import logging
import os

from . import visiongate as vg
from .handler import _read_upload_file

log = logging.getLogger("hermes.plugins.greeninvoice.hooks")

GATED_TOOLS = {"gi_upload_expense_file"}

_announced = False


def _announce_once() -> None:
    """Prove the gate is live — from a HOOK CALL, not from registration.

    We used to log this in register(), and it never appeared: plugin registration runs
    before Hermes attaches its logging handlers, so startup-time lines are swallowed.
    An operational check that can never fire is worse than none, because it reads as
    "the gate is dead" every single time.

    Logging on first invocation is also a strictly stronger claim: it proves the hook is
    actually being CALLED, not merely that register() ran without raising.
    """
    global _announced
    if _announced:
        return
    _announced = True
    vg.audit("live model=%s observe=%s", vg.MODEL, vg.OBSERVE)


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def pre_gateway_dispatch(event=None, **_kw):
    """Pre-warm the classifier for inbound images. Observes only — NEVER rewrites.

    This hook used to append a classification to the message so Elena would know what
    she was looking at. We removed that, and the reason is worth recording.

    Hermes already hands her the raw image natively (OpenAI, image_input_mode=auto), and
    in testing she read a crumpled Hebrew receipt straight off the photo — merchant, tax
    ID, VAT breakdown, total, "paid in cash" — while our classifier's entire contribution
    would have been `kind=receipt confidence=0.98`. The annotation told her nothing she
    could not already see, and it cost us a prompt-injection channel (a model reading an
    attacker-influenceable image, re-emitting text into her context) and a rewrite of
    `event.text` that had to dodge slash-command parsing.

    So the model's job is now purely to ENFORCE, in pre_tool_call, where the agent cannot
    argue with it. This hook exists only to start that classification early, so the gate
    is warm (~0s) rather than cold (~5s) when the upload is actually attempted.

    It runs on the gateway's asyncio EVENT LOOP, so it does exactly one stat() per image
    and hands the real work to a worker thread. Nothing here may block.

    (Caveat worth knowing: a message that arrives while Elena is BUSY is diverted to
    _handle_active_session_busy_message and never reaches this hook at all, so the warm
    never happens — see hermes-busy-mode.sh. Enforcement is unaffected: pre_tool_call
    classifies on demand if the cache is cold.)
    """
    if not vg.ENABLED or event is None:
        return None
    _announce_once()
    try:
        for path in (getattr(event, "media_urls", None) or []):
            if not isinstance(path, str):
                continue
            if _ext(path) not in vg.IMAGE_EXT | vg.PDF_EXT:
                continue
            if vg.lookup_by_path(path) is None:   # stat() only
                vg.warm(path)
    except Exception:
        log.exception("visiongate: pre-warm failed (harmless; the gate classifies on demand)")
    return None                                   # never influences dispatch


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
    _announce_once()
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
