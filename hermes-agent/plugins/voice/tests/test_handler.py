"""Handler + client-contract tests for the voice plugin.

The handler validates input, calls `_client.speak()`, and returns JSON with a
stable shape. We exercise the validation paths directly, and the success /
synth-failure / delivery-failure shapes by monkeypatching the two network
hops in `_client`.
"""

from __future__ import annotations

import json

from hermes_voice_pkg import _client, handler
from hermes_voice_pkg.schemas import MAX_TEXT_CHARS


def speak(args):
    return json.loads(handler.speak_to_david(args))


# --------------------------------------------------------------------------
# input validation (no network)
# --------------------------------------------------------------------------

def test_missing_text_is_invalid():
    resp = speak({})
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert resp["reason"] == "invalid_field_type"


def test_non_string_text_is_invalid():
    resp = speak({"text": 123})
    assert resp["ok"] is False
    assert resp["reason"] == "invalid_field_type"


def test_empty_text_is_invalid():
    resp = speak({"text": "   "})
    assert resp["ok"] is False
    assert resp["reason"] == "empty_text"


def test_text_too_long_is_invalid():
    resp = speak({"text": "x" * (MAX_TEXT_CHARS + 1)})
    assert resp["ok"] is False
    assert resp["reason"] == "text_too_long"


# --------------------------------------------------------------------------
# success + delivery shapes (network hops monkeypatched)
# --------------------------------------------------------------------------

def test_success_delivered(monkeypatch):
    monkeypatch.setattr(_client, "_synthesize", lambda text: b"RIFFfake-wav")
    monkeypatch.setattr(_client, "_deliver", lambda wav: True)
    resp = speak({"text": "Task finished."})
    assert resp == {
        "ok": True, "delivered": True,
        "voice": "devnen-elena", "chars": len("Task finished."),
    }


def test_success_but_mac_unreachable(monkeypatch):
    # Synthesis worked, but the Mac was asleep — best-effort, still ok=true.
    monkeypatch.setattr(_client, "_synthesize", lambda text: b"RIFFfake-wav")
    monkeypatch.setattr(_client, "_deliver", lambda wav: False)
    resp = speak({"text": "Anyone home?"})
    assert resp["ok"] is True
    assert resp["delivered"] is False


def test_text_is_stripped_before_use(monkeypatch):
    captured = {}
    monkeypatch.setattr(_client, "_synthesize",
                        lambda text: captured.setdefault("text", text) or b"w")
    monkeypatch.setattr(_client, "_deliver", lambda wav: True)
    speak({"text": "  hello  "})
    assert captured["text"] == "hello"


# --------------------------------------------------------------------------
# synthesis failure -> ok=false, error=synth_failed
# --------------------------------------------------------------------------

def test_synth_failure_maps_to_ok_false(monkeypatch):
    def boom(text):
        raise _client.SynthError("synth_unreachable", "connection refused")

    monkeypatch.setattr(_client, "_synthesize", boom)
    resp = speak({"text": "hi"})
    assert resp["ok"] is False
    assert resp["error"] == "synth_failed"
    assert resp["reason"] == "synth_unreachable"


def test_deliver_never_raises_on_dead_listener():
    # Real _deliver against the unroutable listener from conftest must return
    # False, not raise. (Uses the real function, no monkeypatch.)
    assert _client._deliver(b"RIFFfake-wav") is False
