"""Voice client — synthesize on the local Jetson TTS server, ship the WAV to
the Mac voice-listener for playback over Tailscale.

This is the Python analogue of the `jetson-voice-say` shell wrapper, living
*inside* Elena's process. We deliberately do NOT shell out to that wrapper:
it lives under /home/dbexpertai/.claude/hooks, which the hardened hermes unit
hides behind ProtectHome=tmpfs (it is literally absent from this process's
filesystem). Reimplementing the two HTTP hops here keeps the tool self-
contained and dependency-free.

No secrets are involved (unlike the mailer, which fronts a credential-holding
daemon), so there is no privilege-separated broker — the hermes sandbox
already permits AF_INET egress, so we just make the two requests directly.

Flow:
  1. POST {"text","voice",...} -> $TTS_URL/synthesize    -> WAV bytes (local, no network)
  2. POST WAV (audio/wav)      -> $LISTENER_URL/play      -> afplay on the Mac

Two distinct failure modes, surfaced differently to the model:
  * synthesis fails  -> ok=false, error=synth_failed   (we couldn't even make
    the audio; the local TTS server is down/broken — worth reporting)
  * delivery fails   -> ok=true,  delivered=false       (audio was made but the
    Mac was unreachable/asleep; best-effort by design, not an error)

Stdlib only (urllib) — the hermes virtualenv is not guaranteed to have
`requests`, and we don't want to add a dependency for two POSTs.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Defaults match jetson-voice-say. Override via env if the topology changes.
DEFAULT_TTS_URL = "http://localhost:18080"
DEFAULT_LISTENER_URL = "http://100.82.188.1:18082"  # Mac Tailscale IP : listener port
DEFAULT_VOICE = "devnen-elena"  # Elena's reserved voice

# Synthesis can take a while for longer text (chunked on the server); give it
# room. Delivery is a small upload to a LAN/Tailscale peer — keep it snappy so
# a sleeping Mac doesn't hang the tool.
SYNTH_TIMEOUT_SECONDS = 60
DELIVER_CONNECT_TIMEOUT_SECONDS = 4
DELIVER_TIMEOUT_SECONDS = 30

# Keep in sync with schemas.MAX_TEXT_CHARS.
MAX_TEXT_CHARS = 800


class SynthError(Exception):
    """The local TTS server could not produce audio (down, error, or empty)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _tts_url() -> str:
    return (os.environ.get("HERMES_VOICE_TTS_URL") or DEFAULT_TTS_URL).rstrip("/")


def _listener_url() -> str:
    return (os.environ.get("HERMES_VOICE_LISTENER_URL")
            or DEFAULT_LISTENER_URL).rstrip("/")


def _voice() -> str:
    return os.environ.get("HERMES_VOICE_NAME") or DEFAULT_VOICE


def _synthesize(text: str) -> bytes:
    """POST the text to the TTS server, return WAV bytes. Raise SynthError."""
    payload = json.dumps({"text": text, "voice": _voice()}).encode("utf-8")
    req = urllib.request.Request(
        f"{_tts_url()}/synthesize",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=SYNTH_TIMEOUT_SECONDS) as resp:
            wav = resp.read()
    except urllib.error.HTTPError as e:
        raise SynthError("synth_http_error", f"{e.code}")
    except urllib.error.URLError as e:
        raise SynthError("synth_unreachable", str(getattr(e, "reason", e)))
    except (TimeoutError, OSError) as e:
        raise SynthError("synth_timeout", str(e))
    if not wav:
        raise SynthError("synth_empty", "server returned no audio")
    return wav


def _deliver(wav: bytes) -> bool:
    """POST the WAV to the Mac listener. Return True if it played, False on any
    delivery problem (Mac asleep / off network / listener down). Never raises —
    delivery is best-effort by contract."""
    req = urllib.request.Request(
        f"{_listener_url()}/play",
        data=wav,
        headers={"Content-Type": "audio/wav"},
        method="POST",
    )
    try:
        # urlopen's single timeout covers connect+read; the short value keeps a
        # dead Mac from stalling the tool for the full read window.
        with urllib.request.urlopen(req, timeout=DELIVER_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def speak(text: str) -> dict:
    """Synthesize `text` in Elena's voice and play it on the Mac.

    Returns a dict (never raises): on success
        {"ok": True, "delivered": <bool>, "voice": ..., "chars": N}
    `delivered` is False when synthesis succeeded but the Mac couldn't be
    reached. On synthesis failure the SynthError propagates to the handler,
    which maps it to ok=false.
    """
    wav = _synthesize(text)  # raises SynthError -> handler maps to ok=false
    delivered = _deliver(wav)
    return {
        "ok": True,
        "delivered": delivered,
        "voice": _voice(),
        "chars": len(text),
    }
