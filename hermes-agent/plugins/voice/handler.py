"""voice plugin handler — thin shim over _client.speak().

Contract (mirrors the mailer handler):
  - Validate input types up-front so simple LLM mistakes never hit the network.
  - Always return a JSON string; never raise.

Response shapes:
  ok:   {"ok": true, "delivered": <bool>, "voice": "devnen-elena", "chars": N}
        delivered=false means the audio was synthesized but the Mac was
        unreachable (asleep / off Tailscale) — best-effort, not an error.
  bad input:  {"ok": false, "error": "invalid_input", "reason": ...}
  synth down: {"ok": false, "error": "synth_failed", "reason": ..., "detail": ...}
"""

from __future__ import annotations

import json

from . import _client
from .schemas import MAX_TEXT_CHARS


def speak_to_david(args: dict, **_kwargs) -> str:
    try:
        text = args.get("text")
    except Exception as e:
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "args_unreadable", "detail": type(e).__name__,
        })

    if not isinstance(text, str):
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "invalid_field_type", "detail": "text",
        })

    text = text.strip()
    if not text:
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "empty_text",
        })

    if len(text) > MAX_TEXT_CHARS:
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "text_too_long", "detail": f"max {MAX_TEXT_CHARS} chars",
        })

    try:
        resp = _client.speak(text)
    except _client.SynthError as e:
        return json.dumps({
            "ok": False, "error": "synth_failed",
            "reason": e.reason, "detail": e.detail,
        })
    except Exception as e:
        # Last-resort safety net — handler MUST always return JSON.
        return json.dumps({
            "ok": False, "error": "invalid_input",
            "reason": "internal_error", "detail": type(e).__name__,
        })

    return json.dumps(resp)
