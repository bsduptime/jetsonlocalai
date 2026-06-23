"""JSON schema exposed to the LLM via ctx.register_tool().

Only `text` is exposed. The *voice* is deliberately NOT a parameter: Elena
always speaks as devnen-elena (her reserved identity — see voice-replies/
voice-profiles). The prosody knobs (exaggeration/cfg_weight/temperature/
speed) are intentionally hidden too — they're operator-tuned defaults in
_client.py, not something the model should fiddle with per-utterance.
"""

from __future__ import annotations

# Keep in sync with _client.MAX_TEXT_CHARS.
MAX_TEXT_CHARS = 800

SPEAK_TO_DAVID = {
    "name": "speak_to_david",
    "description": (
        "Speak a short message OUT LOUD to David through the speakers on his "
        "Mac, in your own voice. Use this when something is worth interrupting "
        "him for and a written reply might be missed: a long task finished, "
        "you hit a blocker and need a decision, or a time-sensitive alert. "
        "This is one-way audio only — there is NO microphone and David cannot "
        "answer back through it, so never ask a question you need answered via "
        "this tool (put questions in your normal text reply instead). Delivery "
        "is best-effort: if the Mac is asleep or off the network the audio is "
        "silently dropped and the tool still returns ok=true with "
        "delivered=false. Keep it to one or two spoken sentences. Returns "
        "ok=false only on bad input or if the local speech synthesizer is "
        "unreachable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "What to say, as plain spoken prose (no markdown, no URLs, "
                    f"no emoji). One or two sentences. Max {MAX_TEXT_CHARS} "
                    "characters."
                ),
                "minLength": 1,
                "maxLength": MAX_TEXT_CHARS,
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}
